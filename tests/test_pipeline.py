"""Pipeline end-to-end on the fake backend: the five stages, degradation, caching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from conftest import FakeBackend, StubSource

from sibilla.config import Config, DeliveryConfig
from sibilla.pipeline import (
    RunReport,
    build_clusters,
    build_map,
    ensure_state,
    fetch_and_normalize,
    judge_pending,
    make_backend,
    run_cycle,
    window_for,
)
from sibilla.sources.base import RawItem, SourceFetchError
from sibilla.store import Store


def test_run_cycle_end_to_end(pipeline_env, tmp_path):
    cfg = pipeline_env
    report = run_cycle(cfg, date_label=datetime.now().strftime("%Y-%m-%d"))
    assert report.judged == 3
    assert report.judge_errors == 0
    assert report.map_path and report.map_path.endswith(".html")
    assert (tmp_path / f"sibilla-{report.date_label}.html").exists()

    store = Store(cfg.db_path)
    try:
        # second cycle: verdict cache key hits, zero re-judging (docs/03)
        report2 = run_cycle(cfg, date_label=report.date_label)
        assert report2.judged == 0
        assert report2.spend_usd == 0.0  # local CLM ledger
        # cross-source echo: arXiv + HN carried the same arXiv id
        assert report2.clusters == 1
    finally:
        store.close()


def test_source_outage_degrades_with_note(pipeline_env, monkeypatch):
    cfg = pipeline_env

    def boom(cfg):
        return StubSource(error=SourceFetchError("503"))

    registry = {
        "arxiv": boom,
        "hackernews": lambda cfg: StubSource([]),
        "reddit": lambda cfg: StubSource([]),
        "x": lambda cfg: StubSource([]),
    }
    monkeypatch.setattr("sibilla.pipeline.sources_registry", lambda: registry)
    report = run_cycle(cfg, date_label=datetime.now().strftime("%Y-%m-%d"))
    assert any("arxiv: unreachable" in o for o in report.outages)
    # the map still renders with the remaining sources + missing-source note (docs/01)
    html = open(report.map_path, encoding="utf-8").read()
    assert "arxiv: unreachable" in html


def test_backend_down_degrades_to_last_verdicts(pipeline_env, monkeypatch, tmp_path):
    cfg = pipeline_env
    first = run_cycle(cfg, date_label=datetime.now().strftime("%Y-%m-%d"))

    class DeadBackend(FakeBackend):
        def health(self):
            return False

    monkeypatch.setattr("sibilla.pipeline.make_backend", lambda cfg: DeadBackend())
    store = Store(cfg.db_path)
    try:
        report = RunReport(date_label=first.date_label, backend_down=True)  # the cycle recorded the outage
        backend = make_backend(cfg)
        assert backend.health() is False
        path = build_map(cfg, store, backend, report)
        html = open(path, encoding="utf-8").read()
        assert "judge backend down" in html  # stale badge, never silent
    finally:
        store.close()


def test_judge_pending_never_mixes_configs(pipeline_env):
    """Per-source requests only (docs/03 scheduling rules)."""
    cfg = pipeline_env
    store = Store(cfg.db_path)
    try:
        fetch_and_normalize(cfg, store, datetime.now(timezone.utc) - timedelta(hours=2))
        backend = FakeBackend()
        judged, errors = judge_pending(cfg, store, backend, "h" * 64, "STATE")
        assert judged == 3 and errors == 0
        assert all(c["items"] <= 3 for c in backend.calls)
    finally:
        store.close()


def test_ensure_state_falls_back_on_gh_failure(pipeline_env, monkeypatch):
    cfg = pipeline_env
    store = Store(cfg.db_path)
    try:
        store.save_state("lastgood", "previous state text")

        def fail(*a, **kw):
            from sibilla.state.builder import StateBuilderError

            raise StateBuilderError("gh auth failed")

        monkeypatch.setattr("sibilla.pipeline.state_builder.build_state", fail)
        state_hash, text, stale = ensure_state(cfg, store, rebuild=True)
        assert state_hash == "lastgood" and stale is True  # loud fallback (docs/01)
    finally:
        store.close()


def test_window_anchored_at_delivery_hour():
    cfg = Config(delivery=DeliveryConfig(hour=6))
    start, end, label = window_for(cfg, "2026-01-10")  # a past date: cutoff is not clamped
    assert end.hour == 6
    assert label == "2026-01-10"
    assert (end - start).total_seconds() == 24 * 3600


def test_source_window_precedence():
    """--source-window > --window-hours > per-source window: > global window:."""
    from sibilla.config import ArxivConfig, RedditConfig, SourcesConfig
    from sibilla.pipeline import source_window_hours

    cfg = Config(
        window_hours=24,
        sources=SourcesConfig(arxiv=ArxivConfig(window_hours=96), reddit=RedditConfig(window_hours=48)),
    )
    assert source_window_hours(cfg, "arxiv") == 96  # per-source config
    assert source_window_hours(cfg, "reddit") == 48
    assert source_window_hours(cfg, "hackernews") == 24  # falls back to global
    assert source_window_hours(cfg, "arxiv", window_hours=168) == 168  # --window-hours beats yaml
    assert source_window_hours(cfg, "reddit", source_overrides={"reddit": 12}) == 12  # --source-window wins
    assert source_window_hours(cfg, "arxiv", window_hours=168, source_overrides={"arxiv": 192}) == 192


def test_fetch_uses_per_source_windows(pipeline_env, sample_raw_items, monkeypatch):
    """arXiv fetches with its own wider window; HN/Reddit with theirs."""
    from sibilla.config import ArxivConfig
    from sibilla.pipeline import fetch_and_normalize

    captured: dict[str, datetime] = {}

    class RecordingStub(StubSource):
        def __init__(self, name, items=None):
            super().__init__(items)
            self.name = name

        def fetch(self, since):
            captured[self.name] = since
            return self.items

    cfg = pipeline_env
    cfg.sources.arxiv = ArxivConfig(window_hours=96)
    registry = {
        "arxiv": lambda cfg: RecordingStub("arxiv", sample_raw_items["arxiv"]),
        "hackernews": lambda cfg: RecordingStub("hackernews", sample_raw_items["hackernews"]),
        "reddit": lambda cfg: RecordingStub("reddit", sample_raw_items["reddit"]),
        "x": lambda cfg: RecordingStub("x", []),
    }
    monkeypatch.setattr("sibilla.pipeline.sources_registry", lambda: registry)

    end = datetime(2026, 9, 27, 6, 0, tzinfo=timezone.utc)
    fetch_and_normalize(cfg, Store(cfg.db_path), end)
    assert captured["arxiv"] == end - timedelta(hours=96)
    assert captured["hackernews"] == end - timedelta(hours=24)
    assert captured["reddit"] == end - timedelta(hours=24)
    # --source-window wins for the named source only
    fetch_and_normalize(cfg, Store(cfg.db_path), end, source_overrides={"arxiv": 192})
    assert captured["arxiv"] == end - timedelta(hours=192)
    assert captured["hackernews"] == end - timedelta(hours=24)


def test_map_includes_arxiv_within_its_own_window(pipeline_env, sample_raw_items, monkeypatch):
    """A paper published 48h ago shows up when arXiv's window is 96h — not at a flat 24h."""
    from sibilla.config import ArxivConfig
    from sibilla.store import Store

    old_paper = RawItem(
        source="arxiv",
        native_id="2609.00042v1",
        title="Weekend paper",
        url="https://arxiv.org/abs/2609.00042v1",
        author="A",
        published=datetime.now(timezone.utc) - timedelta(hours=48),
        body="Listed with a lag.",
    )
    cfg = pipeline_env
    cfg.sources.arxiv = ArxivConfig(window_hours=96)
    registry = {
        "arxiv": lambda cfg: StubSource([old_paper]),
        "hackernews": lambda cfg: StubSource([]),
        "reddit": lambda cfg: StubSource([]),
        "x": lambda cfg: StubSource([]),
    }
    monkeypatch.setattr("sibilla.pipeline.sources_registry", lambda: registry)
    run_cycle(cfg)  # fetch + judge with the per-source windows

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    store = Store(cfg.db_path)
    try:
        import sibilla.pipeline as pl  # resolve the (patched) make_backend at call time

        report = RunReport(date_label=today)
        build_map(cfg, store, pl.make_backend(cfg), report)
        html96 = open(report.map_path, encoding="utf-8").read()
        assert "Weekend paper" in html96  # 48h old, inside arXiv's 96h window

        # a flat 24h window drops it — honest, not hidden
        report24 = RunReport(date_label=today, window_hours=24)
        build_map(cfg, store, pl.make_backend(cfg), report24)
        html24 = open(report24.map_path, encoding="utf-8").read()
        assert "Weekend paper" not in html24
    finally:
        store.close()


def test_window_future_date_clamps_to_now():
    cfg = Config(delivery=DeliveryConfig(hour=6))
    _, end, _ = window_for(cfg, "2099-01-01")
    assert end <= datetime.now().astimezone()  # a future cutoff would shrink the world


def test_clusters_replace_atomicity(pipeline_env):
    cfg = pipeline_env
    store = Store(cfg.db_path)
    try:
        run_cycle(cfg, date_label=datetime.now().strftime("%Y-%m-%d"))
        n1 = store.cluster_counts(0, 2**31)
        assert n1 >= 0
        # rebuilding clusters is idempotent — replace, not append
        build_clusters(cfg, store, None, 0, 2**31)
        assert store.cluster_counts(0, 2**31) == n1
    finally:
        store.close()
