"""Deterministic state builder: GitHub → profile text + state_hash (docs/03).

The state is the judge's input and is assembled in code — the model never
invents the profile. Format (docs/03):

    ACTIVE WORK PROFILE (auto-generated from GitHub, YYYY-MM-DD)

    ## owner/repo — description
    topics: a, b, c
    recent: fix wiki ingestion; add compass scoring; ...

RADAR.md is appended verbatim. ``sha256(state_text)`` keys every verdict.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import requests

from sibilla.config import StateConfig
from sibilla.state.radar_md import load_radar_md

GITHUB_API = "https://api.github.com"
_MARKUP_RE = re.compile(r"[`*_>\[\]()#]+")


class StateBuilderError(RuntimeError):
    """GitHub unreachable / unauthorized — caller falls back to last good state."""


@dataclass
class RepoSummary:
    full_name: str
    description: str
    topics: list[str]
    readme_lead: str
    commit_subjects: list[str]

    def render(self) -> str:
        lines = [
            f"## {self.full_name} — {self.description or self.readme_lead.split('. ')[0] or '(no description)'}".rstrip()
        ]
        if self.topics:
            lines.append(f"topics: {', '.join(self.topics)}")
        if self.readme_lead:
            lines.append(f"readme: {self.readme_lead}")
        if self.commit_subjects:
            lines.append("recent: " + "; ".join(self.commit_subjects))
        return "\n".join(lines)


@dataclass
class State:
    text: str
    state_hash: str
    repos: list[RepoSummary] = field(default_factory=list)
    radar_md_used: bool = False
    built_at: int = field(default_factory=lambda: int(time.time()))


def _headers(token: str | None) -> dict[str, str]:
    h = {"Accept": "application/vnd.github+json", "User-Agent": "sibilla-state-builder"}
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(session: requests.Session, url: str, token: str | None, **params: Any) -> Any:
    resp = session.get(url, headers=_headers(token), params=params or None, timeout=15)
    if resp.status_code == 401:
        raise StateBuilderError(f"GitHub auth failed for {url} (check GITHUB_TOKEN)")
    if resp.status_code == 404:
        return None
    if resp.status_code != 200:
        raise StateBuilderError(f"GitHub API {resp.status_code} for {url}: {resp.text[:200]}")
    return resp.json()


def list_repos(
    session: requests.Session, users: list[str], orgs: list[str], cfg: StateConfig, token: str | None
) -> list[dict[str, Any]]:
    """Repos pushed within the age window, excluding forks and archived (docs/03)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=cfg.max_repo_age_months * 30)
    out: list[dict[str, Any]] = []
    for owner in [*users, *orgs]:
        owner_type = "users" if owner in users else "orgs"
        data = _get(session, f"{GITHUB_API}/{owner_type}/{owner}/repos", token, sort="pushed", per_page=100)
        if data is None:
            continue  # user/org with no repos or renamed — not fatal
        for repo in data:
            if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
                continue
            pushed = repo.get("pushed_at")
            if pushed and datetime.fromisoformat(pushed.replace("Z", "+00:00")) < cutoff:
                continue
            out.append(repo)
    return out


def readme_lead(session: requests.Session, full_name: str, token: str | None, max_chars: int) -> str:
    """README raw, first prose section, headings/markup stripped, ~max_chars (docs/03)."""
    resp = session.get(
        f"{GITHUB_API}/repos/{full_name}/readme",
        headers={**_headers(token), "Accept": "application/vnd.github.raw+json"},
        timeout=15,
    )
    if resp.status_code != 200 or not resp.text:
        return ""
    lines: list[str] = []
    size = 0
    for line in resp.text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):  # drop headings; stop at the second one
            if lines:
                break
            continue
        if not stripped and not lines:
            continue
        clean = _MARKUP_RE.sub("", stripped).strip()
        if clean:
            lines.append(clean)
            size += len(clean) + 1
        if size >= max_chars:
            break
    text = " ".join(lines)
    return text[:max_chars]


def commit_subjects(session: requests.Session, full_name: str, token: str | None, limit: int) -> list[str]:
    data = _get(session, f"{GITHUB_API}/repos/{full_name}/commits", token, per_page=limit)
    if not data:
        return []
    out: list[str] = []
    for c in data:
        msg = (c.get("commit", {}).get("message") or "").splitlines()
        if msg and msg[0].strip():
            out.append(msg[0].strip())
    return out


def build_state(
    cfg: StateConfig, token: str | None = None, session: requests.Session | None = None, today: str | None = None
) -> State:
    """Assemble the state text exactly in the docs/03 shape and hash it."""
    session = session or requests.Session()
    today = today or datetime.now().strftime("%Y-%m-%d")
    repos: list[RepoSummary] = []
    for repo in list_repos(session, cfg.github_users, cfg.github_orgs, cfg, token):
        full = repo.get("full_name") or ""
        if not full:
            continue
        repos.append(
            RepoSummary(
                full_name=full,
                description=(repo.get("description") or "").strip(),
                topics=list(repo.get("topics") or []),
                readme_lead=readme_lead(session, full, token, cfg.readme_chars),
                commit_subjects=commit_subjects(session, full, token, cfg.commits_per_repo),
            )
        )
    repos.sort(key=lambda r: r.full_name)

    parts = [f"ACTIVE WORK PROFILE (auto-generated from GitHub, {today})"]
    for r in repos:
        parts.append("")
        parts.append(r.render())
    radar = load_radar_md(cfg.radar_md)
    if radar:
        parts.append("")
        parts.append("ADDITIONAL INTERESTS (RADAR.md, user-provided)")
        parts.append("")
        parts.append(radar)

    text = "\n".join(parts).strip() + "\n"
    return State(
        text=text,
        state_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        repos=repos,
        radar_md_used=bool(radar),
    )
