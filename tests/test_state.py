"""State builder: deterministic agent conversations → profile text + state_hash."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from sibilla.config import ConversationsConfig, StateConfig
from sibilla.state import builder as state_builder
from sibilla.state.conversations import clean_user_text, redact
from sibilla.state.radar_md import load_radar_md

NOW = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def _write_session(root, project_dir: str, session: str, turns: list[dict], title: str | None = None):
    d = root / project_dir
    d.mkdir(parents=True, exist_ok=True)
    lines = []
    if title:
        lines.append({"type": "ai-title", "title": title, "sessionId": session})
    for t in turns:
        lines.append(t)
    (d / f"{session}.jsonl").write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")


def _user_turn(text: str, minutes_ago: float = 60, session: str = "s1") -> dict:
    return {
        "type": "user",
        "sessionId": session,
        "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "message": {"role": "user", "content": text},
    }


def _assistant_turn(text: str, minutes_ago: float = 30, session: str = "s1") -> dict:
    return {
        "type": "assistant",
        "sessionId": session,
        "timestamp": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


@pytest.fixture
def conv_tree(tmp_path):
    """Two projects: sibilla (busy, recent) + OneRing (older, quieter)."""
    _write_session(
        tmp_path,
        "-home-maeste-project-sibilla",
        "aaa",
        [
            _user_turn("fai una PR con tutte le correzioni", minutes_ago=60),
            _user_turn("non mi piace il 42x, ricalcola i costi reali", minutes_ago=30 * 24 * 60),
        ],
        title="typesafe wire fixes",
    )
    _write_session(
        tmp_path,
        "-home-maeste-project-Prince-OneRing",
        "bbb",
        [_user_turn("sistema il runtime del modulo X", minutes_ago=5 * 24 * 60)],
    )
    # tool results and reminders must not leak into the state
    _write_session(
        tmp_path,
        "-home-maeste-project-sibilla",
        "ccc",
        [
            {
                "type": "user",
                "sessionId": "ccc",
                "timestamp": NOW.isoformat(),
                "message": {"role": "user", "content": [{"type": "tool_result", "content": "huge blob"}]},
            },
        ],
    )
    return tmp_path


def _cfg(conv_tree, **kw) -> StateConfig:
    return StateConfig(
        conversations=ConversationsConfig(roots=[str(conv_tree)], **kw),
        radar_md=str(conv_tree / "absent.md"),
    )


def test_state_format_and_determinism(conv_tree):
    cfg = _cfg(conv_tree)
    s1 = state_builder.build_state(cfg, today="2026-10-01")
    s2 = state_builder.build_state(cfg, today="2026-10-01")
    assert s1.state_hash == s2.state_hash  # deterministic: same input, same hash
    text = s1.text
    assert text.startswith("ACTIVE WORK PROFILE (auto-generated from agent conversations, 2026-10-01)")
    assert "## sibilla" in text and "### typesafe wire fixes" in text  # ai-title as header
    assert "fai una PR" in text
    assert "## Prince-OneRing" in text  # dir name humanized
    assert "huge blob" not in text  # tool results never enter
    assert len(text) < 16000 + 2000  # budget respected (header + radar note slack)


def test_window_excludes_old_turns(conv_tree):
    cfg = _cfg(conv_tree, window_days=1)
    text = state_builder.build_state(cfg, today="2026-10-01").text
    assert "fai una PR" in text  # 1h old
    assert "42x" not in text  # 30 days old — outside the window


def test_exclude_projects(conv_tree):
    cfg = _cfg(conv_tree, exclude_projects=["Prince-OneRing"])
    text = state_builder.build_state(cfg, today="2026-10-01").text
    assert "Prince-OneRing" not in text
    assert "## sibilla" in text


def test_user_only_by_default_assistant_opt_in(conv_tree):
    _write_session(
        conv_tree,
        "-home-maeste-project-sibilla",
        "ddd",
        [_user_turn("domanda nuova", minutes_ago=10), _assistant_turn("risposta lunga dell'agente", minutes_ago=5)],
    )
    only_user = state_builder.build_state(_cfg(conv_tree), today="2026-10-01").text
    assert "domanda nuova" in only_user
    assert "risposta lunga" not in only_user  # default: user prompts carry the intention
    both = state_builder.build_state(_cfg(conv_tree, include="user+assistant"), today="2026-10-01").text
    assert "risposta lunga" in both


def test_proportional_budget_with_cap(conv_tree):
    """One giant project cannot eat the whole budget: ~30% cap per project."""
    big = conv_tree / "-home-maeste-project-giant"
    big.mkdir(parents=True, exist_ok=True)
    _write_session(
        conv_tree,
        "-home-maeste-project-giant",
        "ggg",
        [_user_turn(f"prompt enorme numero {i} " + "x" * 300, minutes_ago=i + 1) for i in range(40)],
    )
    cfg = _cfg(conv_tree, char_budget=4000)
    text = state_builder.build_state(cfg, today="2026-10-01").text
    giant_part = text[text.index("## giant") :] if "## giant" in text else ""
    giant_len = len(giant_part.split("##")[0]) if giant_part else 0
    assert giant_len <= int(4000 * 0.30) + 400  # cap honored (title/line slack)
    assert "## sibilla" in text  # smaller projects still get their share


def test_empty_window_is_explicit_not_silent(tmp_path):
    cfg = _cfg(tmp_path)
    state = state_builder.build_state(cfg, today="2026-10-01")
    assert "no conversations in the window" in state.text
    assert state.conversations == []


def test_radar_md_appended_verbatim(conv_tree):
    radar = conv_tree / "RADAR.md"
    radar.write_text("evaluating vector DBs for a Q4 project", encoding="utf-8")
    cfg = _cfg(conv_tree)
    cfg.radar_md = str(radar)
    state = state_builder.build_state(cfg, today="2026-10-01")
    assert state.radar_md_used is True
    assert "ADDITIONAL INTERESTS (RADAR.md, user-provided)" in state.text
    assert state.text.rstrip().endswith("evaluating vector DBs for a Q4 project")


def test_state_hash_changes_with_radar(conv_tree):
    h1 = state_builder.build_state(_cfg(conv_tree), today="2026-10-01").state_hash
    radar = conv_tree / "RADAR.md"
    radar.write_text("deep-diving context engineering", encoding="utf-8")
    cfg = _cfg(conv_tree)
    cfg.radar_md = str(radar)
    h2 = state_builder.build_state(cfg, today="2026-10-01").state_hash
    assert h1 != h2  # rebuilt state → re-judge under a new key (docs/03)


def test_redaction_strips_shaped_secrets():
    text = "usa sk-ABCDEFGH12345678 e ghp_abcdefghijklmnopqrstuvwxyz poi Bearer abcdefghijklmnopqr"
    out = redact(text)
    assert "sk-ABCDEFGH12345678" not in out and "ghp_" not in out and "Bearer abcdef" not in out
    assert out.count("[REDACTED]") >= 3
    assert redact("password=supersecret123") == "password=[REDACTED]"
    assert redact(".env.production") == "[REDACTED]"
    assert redact("parola normale") == "parola normale"  # no false positives on prose


def test_clean_user_text_unwraps_commands():
    raw = "<command-name>/goal</command-name>\n<command-message>goal</command-message>\n<command-args>implementa tutto</command-args>"
    assert clean_user_text(raw) == "implementa tutto"  # the args are the user's words
    raw2 = "<system-reminder>noise</system-reminder>fai una PR"
    assert clean_user_text(raw2) == "fai una PR"


def test_unreadable_roots_raise_builder_error(tmp_path, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk gone")

    monkeypatch.setattr("sibilla.state.builder.build_conversation_state", boom)
    with pytest.raises(state_builder.StateBuilderError):
        state_builder.build_state(_cfg(tmp_path))


def test_radar_md_placeholder_is_ignored(tmp_path):
    radar = tmp_path / "RADAR.md"
    radar.write_text("# RADAR.md\n", encoding="utf-8")
    assert load_radar_md(radar) == ""
    assert load_radar_md(tmp_path / "missing.md") == ""
    radar.write_text("vector DBs for Q4", encoding="utf-8")
    assert load_radar_md(radar) == "vector DBs for Q4"
