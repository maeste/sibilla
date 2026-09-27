"""Configuration loading for Sibilla.

Single config file ``sibilla.yaml`` (shape per docs/02, judge block per docs/03).
Missing file → defaults; unknown keys are ignored so the file can carry notes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG_PATH = "sibilla.yaml"


@dataclass
class ArxivConfig:
    categories: list[str] = field(default_factory=lambda: ["cs.AI", "cs.CL", "cs.LG"])
    # Per-source fetch/map window (hours). arXiv lists with a lag and goes
    # quiet over weekends — the example config ships 96h here; None falls
    # back to the global `window`.
    window_hours: int | None = None


@dataclass
class HackerNewsConfig:
    min_points: int = 40
    window_hours: int | None = None  # per-source window; None → global `window`


@dataclass
class RedditConfig:
    subreddits: list[str] = field(default_factory=lambda: ["LocalLLaMA", "MachineLearning", "agents"])
    min_upvotes: int = 50
    window_hours: int | None = None  # per-source window; None → global `window`


@dataclass
class XConfig:
    profiles: list[str] = field(default_factory=list)
    transport: str = "auto"  # api | bridge | export | auto | off
    bridge_url: str | None = None
    export_path: str | None = None
    bearer_token_env: str = "X_BEARER_TOKEN"
    window_hours: int | None = None  # per-source window; None → global `window`


@dataclass
class SourcesConfig:
    arxiv: ArxivConfig = field(default_factory=ArxivConfig)
    hackernews: HackerNewsConfig = field(default_factory=HackerNewsConfig)
    reddit: RedditConfig = field(default_factory=RedditConfig)
    x: XConfig = field(default_factory=XConfig)
    enabled: dict[str, bool] = field(default_factory=dict)  # per-source enable/disable

    def is_enabled(self, name: str) -> bool:
        return self.enabled.get(name, True)


@dataclass
class StateConfig:
    github_users: list[str] = field(default_factory=list)
    github_orgs: list[str] = field(default_factory=list)
    github_repos: list[str] = field(
        default_factory=list
    )  # explicit owner/repo — always included, bypasses age/fork/archive filters
    github_api_url: str = "https://api.github.com"  # point at GitHub Enterprise: https://ghe.example.com/api/v3
    radar_md: str = "RADAR.md"
    max_repo_age_months: int = 6
    readme_chars: int = 800
    commits_per_repo: int = 20


@dataclass
class JudgeConfigSettings:
    backend: str = "clm"  # clm | typesafe (per docs/03)
    # None → each backend's default (CLM: http://127.0.0.1:8700,
    # typesafe: https://api.typesafe.ai). A CLM-specific default here would
    # silently point a typesafe backend at localhost (dogfooding bug).
    base_url: str | None = None
    api_key_env: str | None = "CLM_API_KEY"
    model: str = "clm-latest"
    temperature: float = 1.0
    concurrency: int = 4  # fan-out workers (CLM shape)
    pack_size: int = 170  # items per request (packed shape, TypeSafe Jev)
    retries: int = 2
    timeout_s: float = 30.0
    input_cost_per_mtok: float | None = None  # override backend default ($0.042 for Jev)
    rank_threshold: float = 0.75  # semantic dedup join probability
    semantic_dedup: bool = False  # v2 feature flag
    # CLM fine-tune loop (docs/03): command template run by `tune --train`.
    # Placeholders: {dataset} (typed decisions JSON) and {head_out} (where the
    # trained head lands, e.g. CLM's train/finetune.py output).
    finetune_cmd: str | None = None
    ckpt_dir: str | None = None  # clm-serve --ckpt-dir; the new head is dropped here


@dataclass
class DeliveryConfig:
    channel: str = "none"  # none | telegram
    token_env: str = "TELEGRAM_BOT_TOKEN"
    chat_id: str | None = None
    hour: int = 6  # daily cutoff hour anchoring the 24h map window


@dataclass
class Config:
    window_hours: int = 24
    db_path: str = "sibilla.db"
    output_dir: str = "."
    radar_md: str = "RADAR.md"
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    state: StateConfig = field(default_factory=StateConfig)
    judge: JudgeConfigSettings = field(default_factory=JudgeConfigSettings)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    path: Path | None = None  # where this config was loaded from


def _build_dataclass(cls: type, data: dict[str, Any] | None):
    """Instantiate a config dataclass from a dict, ignoring unknown keys."""
    if not data:
        return cls()
    known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
    return cls(**{k: v for k, v in data.items() if k in known})


def load_config(path: str | Path | None = None) -> Config:
    """Load sibilla.yaml; fall back to defaults when the file is absent."""
    cfg = Config()
    p = Path(path) if path else Path(DEFAULT_CONFIG_PATH)
    if p.exists():
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"{p}: config root must be a mapping")
        cfg.path = p
        cfg.window_hours = int(raw.get("window", cfg.window_hours))
        cfg.db_path = str(raw.get("db_path", cfg.db_path))
        cfg.output_dir = str(raw.get("output_dir", cfg.output_dir))
        cfg.radar_md = str(raw.get("radar_md", cfg.radar_md))
        cfg.sources = _build_sources(raw.get("sources"))
        cfg.state = _build_dataclass(StateConfig, raw.get("state"))
        cfg.judge = _build_dataclass(JudgeConfigSettings, raw.get("judge"))
        cfg.delivery = _build_dataclass(DeliveryConfig, raw.get("delivery"))
    if not cfg.state.radar_md:
        cfg.state.radar_md = cfg.radar_md
    return cfg


def _build_sources(data: dict[str, Any] | None) -> SourcesConfig:
    src = SourcesConfig()
    if not data:
        return src
    known = {"arxiv", "hackernews", "reddit", "x", "enabled"}
    for key, value in data.items():
        if key in ("arxiv", "hackernews", "reddit", "x"):
            block = dict(value or {})
            if "window" in block:  # YAML spells it `window:`; the field is window_hours
                block["window_hours"] = block.pop("window")
            setattr(
                src,
                key,
                _build_dataclass(
                    {"arxiv": ArxivConfig, "hackernews": HackerNewsConfig, "reddit": RedditConfig, "x": XConfig}[key],
                    block,
                ),
            )
        elif key == "enabled":
            src.enabled = {str(k): bool(v) for k, v in (value or {}).items()}
        elif key not in known:
            continue  # tolerate notes/future keys
    return src


def source_names() -> list[str]:
    """Canonical source names, in roadmap order."""
    return ["arxiv", "hackernews", "reddit", "x"]
