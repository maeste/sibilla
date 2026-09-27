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
