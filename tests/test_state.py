"""State builder: deterministic GitHub → profile text + state_hash."""

from __future__ import annotations

import pytest

from sibilla.config import StateConfig
from sibilla.state import builder as state_builder
from sibilla.state.radar_md import load_radar_md


def test_state_format_matches_docs(tmp_path, github_mock):
    cfg = StateConfig(github_users=["maeste"], radar_md=str(tmp_path / "absent.md"))
    state = state_builder.build_state(cfg)
    assert state.state_hash == state_builder.build_state(cfg).state_hash  # deterministic
    text = state.text
    assert text.startswith("ACTIVE WORK PROFILE (auto-generated from GitHub, ")
    assert "## maeste/radar-core — personal radar engine" in text
    assert "topics: agents, arxiv" in text
    assert "recent: fix wiki ingestion; add compass scoring" in text
    # archived repo excluded by design
    assert "old-thing" not in text
    # README lead, headings/markup stripped, ~800 chars cap
    assert "Personal knowledge base for agents." in text
    assert "# Title" not in text


def test_explicit_repos_bypass_filters(tmp_path, github_mock):
    """github_repos entries are included even if old/archived, and dedup against listings."""
    cfg = StateConfig(
        github_users=["maeste"],
        github_repos=["maeste/secret-sauce", "maeste/old-thing", "maeste/radar-core"],
        radar_md=str(tmp_path / "absent.md"),
    )
    state = state_builder.build_state(cfg)
    text = state.text
    # old + not in listing: only reachable via explicit config — proves the bypass
    assert "## maeste/secret-sauce — private experiments" in text
    # archived listing repo, explicitly requested: filter bypassed, included once
    assert "## maeste/old-thing — archived" in text
    assert text.count("## maeste/radar-core") == 1  # dedup against the user listing
    names = [r.full_name for r in state.repos]
    assert len(names) == len(set(names))


def test_explicit_repo_invalid_name_raises(tmp_path, github_mock):
    cfg = StateConfig(github_repos=["just-a-name"], radar_md=str(tmp_path / "absent.md"))
    with pytest.raises(state_builder.StateBuilderError, match="owner/repo"):
        state_builder.build_state(cfg)


def test_explicit_repo_unknown_is_skipped(tmp_path, github_mock):
    cfg = StateConfig(github_repos=["maeste/ghost-repo"], radar_md=str(tmp_path / "absent.md"))
    state = state_builder.build_state(cfg)  # 404 → skipped, no raise
    assert state.repos == []


def test_custom_github_api_url(tmp_path, github_mock, monkeypatch):
    """GHES: state.github_api_url routes every call (listing, readme, commits)."""
    seen: list[str] = []
    real_get = state_builder.requests.Session.get

    def spying_get(self, url, **kw):
        seen.append(url)
        return real_get(self, url, **kw)

    monkeypatch.setattr(state_builder.requests.Session, "get", spying_get)
    cfg = StateConfig(
        github_users=["maeste"],
        github_api_url="https://ghe.example.com/api/v3",
        radar_md=str(tmp_path / "absent.md"),
    )
    state_builder.build_state(cfg)
    assert seen, "builder made no calls"
    assert all(u.startswith("https://ghe.example.com/api/v3/") for u in seen)


def test_radar_md_appended_verbatim(tmp_path, github_mock):
    radar = tmp_path / "RADAR.md"
    radar.write_text("evaluating vector DBs for a Q4 project", encoding="utf-8")
    cfg = StateConfig(github_users=["maeste"], radar_md=str(radar))
    state = state_builder.build_state(cfg)
    assert state.radar_md_used is True
    assert "ADDITIONAL INTERESTS (RADAR.md, user-provided)" in state.text
    assert state.text.rstrip().endswith("evaluating vector DBs for a Q4 project")


def test_state_hash_changes_with_radar(tmp_path, github_mock):
    cfg = StateConfig(github_users=["maeste"], radar_md=str(tmp_path / "absent.md"))
    h1 = state_builder.build_state(cfg).state_hash
    (tmp_path / "RADAR.md").write_text("deep-diving context engineering", encoding="utf-8")
    cfg.radar_md = str(tmp_path / "RADAR.md")
    h2 = state_builder.build_state(cfg).state_hash
    assert h1 != h2  # rebuilt state → re-judge under a new key (docs/03)


def test_github_auth_failure_raises_builder_error(monkeypatch, tmp_path):
    import requests

    class FailResp:
        status_code = 401
        text = "Bad credentials"

        def json(self):
            return {}

    monkeypatch.setattr(requests.Session, "get", lambda self, url, **kw: FailResp())
    cfg = StateConfig(github_users=["maeste"], radar_md=str(tmp_path / "x.md"))
    with pytest.raises(state_builder.StateBuilderError):
        state_builder.build_state(cfg)


def test_radar_md_placeholder_is_ignored(tmp_path):
    radar = tmp_path / "RADAR.md"
    radar.write_text("# RADAR.md\n", encoding="utf-8")
    assert load_radar_md(radar) == ""
    assert load_radar_md(tmp_path / "missing.md") == ""
    radar.write_text("vector DBs for Q4", encoding="utf-8")
    assert load_radar_md(radar) == "vector DBs for Q4"
