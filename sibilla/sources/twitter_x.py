"""X (Twitter) source — v2, behind the go/no-go gate (docs/02, risk R1).

Content: monitored profile list (the accounts *you* chose to follow).
The API pricing (~$200/mo class) is the real constraint, so the transport
is pluggable by design and the decision is a config choice, not a port:

- ``api``    X API v2 user timelines (bearer token from env).
- ``bridge`` RSS from a self-hosted bridge (RSSHub / nitter-class), where
             legal and reliable — {bridge_url}/{profile}/rss or .xml.
- ``export`` manual export file (JSON list of tweets) from disk.
- ``auto``   first configured transport in the order api → bridge → export.

An unreachable/misconfigured transport returns [] — X is additive, never
load-bearing. JudgeConfig focus: hype filtering, aggressive ~90% KILL.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

from sibilla.config import XConfig
from sibilla.judge.configs import X_CONFIG
from sibilla.sources.base import RawItem

API_BASE = "https://api.x.com/2"
RSS_TITLE_LEN = 280


class XSource:
    name = "x"
    judge_config = X_CONFIG

    def __init__(self, cfg: XConfig | None = None, session: requests.Session | None = None):
        self.cfg = cfg or XConfig()
        self.session = session or requests.Session()

    def fetch(self, since: datetime) -> list[RawItem]:
        since = since.astimezone(timezone.utc)
        transport = self._resolve_transport()
        if transport == "api":
            return self._fetch_api(since)
        if transport == "bridge":
            return self._fetch_bridge(since)
        if transport == "export":
            return self._fetch_export(since)
        return []  # transport=off or nothing configured: the gate stays closed

    # --------------------------------------------------------- transports

    def _resolve_transport(self) -> str | None:
        t = (self.cfg.transport or "auto").lower()
        if t == "off":
            return None
        if t in ("api", "bridge", "export"):
            return t
        # auto: api → bridge → export
        if os.environ.get(self.cfg.bearer_token_env):
            return "api"
        if self.cfg.bridge_url:
            return "bridge"
        if self.cfg.export_path:
            return "export"
        return None

    def _fetch_api(self, since: datetime) -> list[RawItem]:
        token = os.environ.get(self.cfg.bearer_token_env)
        if not token or not self.cfg.profiles:
            return []
        headers = {"Authorization": f"Bearer {token}"}
        out: list[RawItem] = []
        for profile in self.cfg.profiles:
            user_id = self._user_id(profile, headers)
            if user_id is None:
                continue
            params: dict[str, Any] = {
                "max_results": 100,
                "start_time": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "tweet.fields": "created_at,author_id,context_annotations",
                "exclude": "replies",
            }
            try:
                resp = self.session.get(
                    f"{API_BASE}/users/{user_id}/tweets", headers=headers, params=params, timeout=20
                )
            except requests.RequestException:
                continue
            if resp.status_code != 200:
                continue
            for tweet in resp.json().get("data") or []:
                out.append(_tweet_to_item(profile, tweet))
        return out

    def _user_id(self, profile: str, headers: dict[str, str]) -> str | None:
        try:
            resp = self.session.get(f"{API_BASE}/users/by/username/{profile}", headers=headers, timeout=15)
        except requests.RequestException:
            return None
        if resp.status_code != 200:
            return None
        data = resp.json().get("data") or {}
        return data.get("id")

    def _fetch_bridge(self, since: datetime) -> list[RawItem]:
        if not self.cfg.bridge_url or not self.cfg.profiles:
            return []
        out: list[RawItem] = []
        base = self.cfg.bridge_url.rstrip("/")
        for profile in self.cfg.profiles:
            for suffix in ("/rss", ".xml", ""):
                try:
                    resp = self.session.get(f"{base}/{profile}{suffix}", timeout=20)
                    if resp.status_code == 200 and "<rss" in resp.text[:400]:
                        out.extend(_rss_to_items(profile, resp.text, since))
                        break
                except requests.RequestException:
                    continue
        return out

    def _fetch_export(self, since: datetime) -> list[RawItem]:
        """Manual export: JSON list of tweets or CSV (id,author,created_at,text,url)."""
        path = Path(self.cfg.export_path) if self.cfg.export_path else None
        if path is None or not path.exists() or not self.cfg.profiles:
            return []
        wanted = {p.lstrip("@").lower() for p in self.cfg.profiles}
        out: list[RawItem] = []
        if path.suffix.lower() == ".json":
            records = json.loads(path.read_text(encoding="utf-8"))
            for rec in records if isinstance(records, list) else []:
                author = str(rec.get("author") or rec.get("username") or "").lstrip("@").lower()
                if author and author not in wanted:
                    continue
                out.append(_tweet_to_item(author, rec))
        else:
            with path.open(newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh):
                    author = str(row.get("author") or row.get("username") or "").lstrip("@").lower()
                    if author and author not in wanted:
                        continue
                    out.append(_tweet_to_item(author, dict(row)))
        cutoff = since.astimezone(timezone.utc)
        return [i for i in out if i.published is None or i.published >= cutoff]


def _tweet_to_item(author: str, tweet: dict[str, Any]) -> RawItem:
    tweet_id = str(tweet.get("id") or tweet.get("id_str") or tweet.get("conversation_id") or "")
    created = _parse_ts(tweet.get("created_at"))
    text = (tweet.get("text") or tweet.get("full_text") or tweet.get("title") or "").strip()
    quoted = tweet.get("quoted_tweet") or tweet.get("quoted_status")
    if isinstance(quoted, dict) and quoted.get("text"):
        text += f"\n[quoted @{quoted.get('author', '')}: {quoted['text']}]"
    # deterministic fallback id (no hash() — it is salted per process)
    fallback = hashlib.sha256(f"{author}:{text}".encode()).hexdigest()[:12]
    return RawItem(
        source="x",
        native_id=tweet_id or fallback,
        title=text[:RSS_TITLE_LEN],
        url=tweet.get("url") or (f"https://x.com/{author}/status/{tweet_id}" if tweet_id else None),
        author=author,
        published=created,
        body=text,
        meta={"profile": author.lstrip("@")},
    )


def _rss_to_items(profile: str, xml_text: str, since: datetime) -> list[RawItem]:
    """Parse bridge RSS (nitter/RSSHub shape): item title = tweet text."""
    out: list[RawItem] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return out
    for entry in root.iter("item"):
        title = " ".join((entry.findtext("title") or "").split())
        link = (entry.findtext("link") or "").strip()
        pub = entry.findtext("pubDate")
        desc = " ".join((entry.findtext("description") or "").split())
        text = title or desc[:RSS_TITLE_LEN]
        if not text:
            continue
        tweet_id = link.rstrip("/").rsplit("/", 1)[-1] if link else ""
        fallback = hashlib.sha256(f"{profile}:{text}".encode()).hexdigest()[:12]
        out.append(
            RawItem(
                source="x",
                native_id=tweet_id or fallback,
                title=text[:RSS_TITLE_LEN],
                url=link or None,
                author=profile,
                published=_parse_rfc822(pub),
                body=desc or text,
                meta={"profile": profile.lstrip("@")},
            )
        )
    return [i for i in out if i.published is None or i.published >= since]


def _parse_ts(value: object) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _parse_rfc822(value: str | None) -> datetime | None:
    """RSS pubDate, e.g. 'Tue, 26 Sep 2026 08:15:00 GMT'."""
    if not value:
        return None
    from email.utils import parsedate_to_datetime

    try:
        return parsedate_to_datetime(value).astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None
