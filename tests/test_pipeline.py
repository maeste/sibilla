"""Pipeline end-to-end on the fake backend: the five stages, degradation, caching."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from conftest import FakeBackend

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


class StubSource:
    name = "stub"

    def __init__(self, items=None, error: Exception | None = None):
        self.items = items or []
        self.error = error

    def fetch(self, since):
        if self.error:
            raise self.error
        return self.items


def _raw(native_id, source, title, url, body, age_h=1.0):
    ts = datetime.now(timezone.utc).timestamp() - age_h * 3600
    return RawItem(
        source=source,
        native_id=native_id,
        title=title,
        url=url,
        author="a",
        published=datetime.fromtimestamp(ts, tz=timezone.utc),
        body=body,
    )


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch, sample_raw_items):
    cfg = Config(db_path=str(tmp_path / "p.db"), output_dir=str(tmp_path))
    registry = {
        "arxiv": lambda cfg: StubSource(sample_raw_items["arxiv"]),
        "hackernews": lambda cfg: StubSource(sample_raw_items["hackernews"]),
        "reddit": lambda cfg: StubSource(sample_raw_items["reddit"]),
        "x": lambda cfg: StubSource([]),
    }
    monkeypatch.setattr("sibilla.pipeline.sources_registry", lambda: registry)
    monkeypatch.setattr("sibilla.pipeline.make_backend", lambda cfg: FakeBackend())
    monkeypatch.setattr("sibilla.pipeline.state_builder.build_state", lambda *a, **kw: _fake_state())
    return cfg


class _FakeState:
    state_hash = "deadbeef" * 8
    text = "ACTIVE WORK PROFILE (fake)"

    def __init__(self):
        self.repos = []
        self.radar_md_used = False


def _fake_state():
    return _FakeState()


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
