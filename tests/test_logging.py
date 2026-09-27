"""Progress logging: a long run must show it is going (and where it stops)."""

from __future__ import annotations

import logging

from conftest import FakeBackend

import sibilla.cli as cli_mod
from sibilla.judge import configs as jcfgs
from sibilla.judge.packer import JudgeJob, Packer


def test_packer_logs_start_milestones_and_summary(caplog):
    """25 items → start line, ≤10 milestones, ok/errors line with timing."""
    backend = FakeBackend()
    job = JudgeJob(source="arxiv", config=jcfgs.get_config("arxiv"))
    for i in range(25):
        job.add(f"arxiv:{i}", f"item {i}")

    with caplog.at_level(logging.INFO, logger="sibilla.judge.packer"):
        Packer(backend, ledger=lambda **kw: None).run("STATE", [job])

    lines = [r.getMessage() for r in caplog.records]
    assert any("judge arxiv: 25 items (fan-out ×4)" in m for m in lines)
    milestones = [m for m in lines if "/25" in m]
    assert milestones, "a 25-item job must show progress milestones"
    assert all("judge arxiv:" in m for m in milestones)
    assert any("done in" in m and "25 ok, 0 errors" in m for m in lines)


def test_packer_progress_shows_errors(caplog):
    backend = FakeBackend(fail_ids={"poison"})
    job = JudgeJob(source="hackernews", config=jcfgs.get_config("hackernews"))
    for i in range(20):
        job.add(f"hn:{i}", "poison item" if i == 3 else f"item {i}")

    with caplog.at_level(logging.INFO, logger="sibilla.judge.packer"):
        Packer(backend, ledger=lambda **kw: None).run("STATE", [job])

    summary = [r.getMessage() for r in caplog.records if "done in" in r.getMessage()]
    assert summary and "19 ok, 1 errors" in summary[-1]


def test_small_jobs_do_not_spam_milestones(caplog):
    """<20 items: start + summary only — a run of a few items stays readable."""
    backend = FakeBackend()
    job = JudgeJob(source="reddit", config=jcfgs.get_config("reddit"))
    job.add("reddit:1", "only item")

    with caplog.at_level(logging.INFO, logger="sibilla.judge.packer"):
        Packer(backend, ledger=lambda **kw: None).run("STATE", [job])

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 2  # start + done
    assert not any("/1" in m for m in messages)


def test_pipeline_stage_beats(pipeline_env, caplog):
    """fetch/state/dedup/map each leave one INFO line — the run's skeleton."""
    import sibilla.pipeline as pl

    with caplog.at_level(logging.INFO, logger="sibilla.pipeline"):
        pl.run_cycle(pipeline_env, date_label="2026-09-27")

    messages = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("fetch arxiv: window") for m in messages)
    assert any(m.startswith("fetch arxiv:") and "new items in" in m for m in messages)
    assert any(m.startswith("state: ") and "current" in m for m in messages)
    assert any(m.startswith("dedup: ") for m in messages)
    assert any(m.startswith("map: ") for m in messages)


def test_setup_logging_levels(monkeypatch):
    """INFO by default, --verbose → DEBUG, SIBILLA_LOG overrides both."""
    import io

    stream = io.StringIO()
    for kwargs, expected in (({"verbose": False}, logging.INFO), ({"verbose": True}, logging.DEBUG)):
        monkeypatch.delenv("SIBILLA_LOG", raising=False)
        cli_mod._setup_logging(**kwargs)
        assert logging.getLogger("sibilla").getEffectiveLevel() == expected
    monkeypatch.setenv("SIBILLA_LOG", "warning")
    cli_mod._setup_logging(verbose=False)
    assert logging.getLogger("sibilla").getEffectiveLevel() == logging.WARNING
    assert stream  # silence linters on the unused helper
