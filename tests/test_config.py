"""Config loading: docs/02 shape, judge block, defaults when the file is absent."""

from __future__ import annotations

from sibilla.config import load_config

YAML = """
window: 12
db_path: /tmp/radar.db
output_dir: /tmp/maps
sources:
  arxiv:
    categories: [cs.AI]
  hackernews:
    min_points: 100
  reddit:
    subreddits: [LocalLLaMA]
    min_upvotes: 10
  x:
    profiles: [karpathy]
    transport: export
    export_path: /tmp/tweets.json
  enabled:
    reddit: false
state:
  github_users: [maeste]
  github_orgs: [RisorseArtificiali]
  radar_md: RADAR.md
judge:
  backend: typesafe
  base_url: https://api.typesafe.ai
  api_key_env: TYPESAFE_API_KEY
  model: jev-latest
  semantic_dedup: true
delivery:
  channel: telegram
  chat_id: "12345"
"""


def test_full_config_roundtrip(tmp_path):
    p = tmp_path / "sibilla.yaml"
    p.write_text(YAML, encoding="utf-8")
    cfg = load_config(p)
    assert cfg.window_hours == 12
    assert cfg.db_path == "/tmp/radar.db"
    assert cfg.sources.arxiv.categories == ["cs.AI"]
    assert cfg.sources.hackernews.min_points == 100
    assert cfg.sources.reddit.min_upvotes == 10
    assert cfg.sources.is_enabled("reddit") is False
    assert cfg.sources.is_enabled("arxiv") is True  # default on
    assert cfg.sources.x.profiles == ["karpathy"]
    assert cfg.state.github_users == ["maeste"]
    assert cfg.state.github_orgs == ["RisorseArtificiali"]
    assert cfg.judge.backend == "typesafe"
    assert cfg.judge.model == "jev-latest"
    assert cfg.judge.semantic_dedup is True
    assert cfg.delivery.channel == "telegram"
    assert cfg.delivery.chat_id == "12345"


def test_missing_file_uses_defaults(tmp_path):
    cfg = load_config(tmp_path / "nope.yaml")
    assert cfg.window_hours == 24
    assert cfg.judge.backend == "clm"
    assert cfg.judge.base_url == "http://127.0.0.1:8700"
    assert cfg.sources.arxiv.categories == ["cs.AI", "cs.CL", "cs.LG"]


def test_unknown_keys_tolerated(tmp_path):
    p = tmp_path / "sibilla.yaml"
    p.write_text("sources:\n  future_thing: 1\njudge:\n  note: hi\n", encoding="utf-8")
    cfg = load_config(p)  # must not raise
    assert cfg.judge.backend == "clm"


def test_per_source_windows(tmp_path):
    """`window:` inside a source block → window_hours (docs/02 example)."""
    p = tmp_path / "sibilla.yaml"
    p.write_text(
        "window: 24\nsources:\n  arxiv:\n    window: 96\n  hackernews:\n    window: 24\n",
        encoding="utf-8",
    )
    cfg = load_config(p)
    assert cfg.sources.arxiv.window_hours == 96
    assert cfg.sources.hackernews.window_hours == 24
    assert cfg.sources.reddit.window_hours is None  # falls back to global
    assert cfg.window_hours == 24
