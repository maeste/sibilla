"""CLI: run/map/state/tune/revive over the fake backend."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from conftest import FakeBackend

import sibilla.cli as cli_mod
import sibilla.pipeline as pipeline_mod
from sibilla.cli import main
from sibilla.store import Store


@pytest.fixture
def cli_env(tmp_path, monkeypatch, sample_raw_items):
    """A working directory with a sibilla.yaml + stubbed sources/backend/state."""
    (tmp_path / "sibilla.yaml").write_text(
        f"db_path: {tmp_path / 'cli.db'}\noutput_dir: {tmp_path}\n", encoding="utf-8"
    )
    monkeypatch.chdir(tmp_path)
    registry = {
        "arxiv": lambda cfg: _Stub(sample_raw_items["arxiv"]),
        "hackernews": lambda cfg: _Stub(sample_raw_items["hackernews"]),
        "reddit": lambda cfg: _Stub(sample_raw_items["reddit"]),
        "x": lambda cfg: _Stub([]),
    }
    monkeypatch.setattr(pipeline_mod, "sources_registry", lambda: registry)
    monkeypatch.setattr(pipeline_mod, "make_backend", lambda cfg: FakeBackend())
    monkeypatch.setattr(cli_mod, "make_backend", lambda cfg: FakeBackend())
    monkeypatch.setattr(pipeline_mod.state_builder, "build_state", lambda *a, **kw: _FakeState("cafe" * 16))
    return tmp_path


class _Stub:
    def __init__(self, items):
        self.items = items

    def fetch(self, since):
        return self.items


class _FakeState:
    def __init__(self, state_hash):
        self.state_hash = state_hash
        self.text = "ACTIVE WORK PROFILE (fake)"
        self.repos = []
        self.radar_md_used = False


def test_run_and_map_and_revive(cli_env, capsys):
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert main(["run"]) == 0
    out = capsys.readouterr().out
    assert "judged 3" in out

    # map is a pure function of the DB: rebuild from the store alone
    assert main(["map", "--date", today]) == 0
    assert f"map → {cli_env}/sibilla-{today}.html" in capsys.readouterr().out

    # revive: false-negative telemetry into the labels log
    assert main(["revive", "reddit:r1", "--reason", "actually mattered"]) == 0
    store = Store(cli_env / "cli.db")
    try:
        labels = store.labels(source="reddit")
        assert labels and labels[0]["label"] == "revive"
    finally:
        store.close()


def test_tune_import_export_fit_loop(cli_env, tmp_path, capsys):
    main(["run"])  # judge items
    capsys.readouterr()

    store = Store(tmp_path / "cli.db")
    try:
        items = store.items_between(0, 2**31)
        assert len(items) == 3
        rows = [store.conn.execute("SELECT scores FROM verdicts WHERE item_id = ?", (i.id,)).fetchone() for i in items]
        assert all(r is not None for r in rows)
    finally:
        store.close()

    # the map's "export labels" button produces this shape
    labels_path = tmp_path / "labels.json"
    labels_path.write_text(
        json.dumps(
            {
                "date": "x",
                "labels": [
                    {"item_id": "arxiv:2609.77777v1", "label": "read", "ts": "t"},
                    {"item_id": "hackernews:999", "label": "skim", "ts": "t"},
                    {"item_id": "reddit:r1", "label": "kill", "ts": "t"},
                    {"item_id": "hackernews:999", "label": "kill", "ts": "t2"},
                    {"item_id": "reddit:r1", "label": "skim", "ts": "t3"},
                    {"item_id": "arxiv:2609.77777v1", "label": "kill", "ts": "t4"},
                ],
            }
        ),
        encoding="utf-8",
    )
    assert main(["tune", "--import-labels", str(labels_path)]) == 0
    assert "imported 6 new labels" in capsys.readouterr().out

    # fit needs ≥5 labelled arXiv items with verdicts — seed them through the store
    store = Store(tmp_path / "cli.db")
    try:
        from sibilla.judge import configs as jcfgs
        from sibilla.store import VerdictKey, VerdictRow

        key = VerdictKey("cafe" * 16, jcfgs.get_config("arxiv").version, "fake", "fake-latest")
        for i in range(6):
            iid = f"arxiv:seed{i}"
            store.upsert_item(_seed_item(iid))
            store.save_verdict(
                VerdictRow(
                    item_id=iid,
                    key=key,
                    scores={"relevance": 8, "novelty": 0.5},
                    confidences={"relevance": 0.7, "novelty": 0.7},
                )
            )
            store.add_label(iid, "read", state_hash=key.state_hash)
    finally:
        store.close()

    assert main(["tune", "--export", str(tmp_path / "ds.json"), "--source", "arxiv"]) == 0
    dataset = json.loads((tmp_path / "ds.json").read_text(encoding="utf-8"))
    assert len(dataset["decisions"]) >= 7
    assert dataset["states"]  # state payloads exported for train/finetune.py

    assert main(["tune", "--fit"]) == 0  # fits gates per (backend, model, jc_version)
    store = Store(tmp_path / "cli.db")
    try:
        assert store.calibration("fake", "fake-latest", "1") is not None
    finally:
        store.close()


def _seed_item(iid: str):
    from sibilla.store import ItemRow

    return ItemRow(
        id=iid,
        source="arxiv",
        native_id=iid.split(":", 1)[1],
        fetched_at=0,
        title=f"seed paper {iid}",
        url=f"https://arxiv.org/abs/{iid}",
        published=0,
        body=f"seed paper {iid} body",
    )


def test_state_command(cli_env, capsys):
    assert main(["state", "--rebuild", "--show"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("state cafe")
    assert "ACTIVE WORK PROFILE" in out


def test_drift_view_after_run(cli_env, capsys):
    main(["run"])
    capsys.readouterr()
    assert main(["state", "--history"]) == 0
    out = capsys.readouterr().out
    assert "backend/model" in out and "READ" in out and "cafe" in out  # state hash + quadrant mix


def test_weekly_recap_window(cli_env, capsys):
    """`run --window-hours 168` = the weekly recap (docs/04 daily flow)."""
    assert main(["run", "--window-hours", "168"]) == 0
    out = capsys.readouterr().out
    assert "judged 3" in out
    assert main(["map", "--window-hours", "168", "--date", datetime.now(timezone.utc).strftime("%Y-%m-%d")]) == 0


def test_source_window_parsing():
    from sibilla.cli import parse_source_window

    assert parse_source_window(None) == {}
    assert parse_source_window("arxiv=192") == {"arxiv": 192}
    assert parse_source_window("arxiv=192, hackernews=48") == {"arxiv": 192, "hackernews": 48}
    for bad in ("arxiv", "arxiv=", "=192", "arxiv=0", "arxiv=-5", "nope=24", "arxiv=abc"):
        with pytest.raises(SystemExit):
            parse_source_window(bad)


def test_cold_start_source_window_flag(cli_env, capsys, monkeypatch):
    """`run --source-window arxiv=192` reaches the pipeline as a per-source override."""
    captured: dict = {}

    real_run_cycle = pipeline_mod.run_cycle

    def spy(cfg, **kw):
        captured.update(kw)
        return real_run_cycle(cfg, **kw)

    monkeypatch.setattr(cli_mod, "run_cycle", spy)
    assert main(["run", "--source-window", "arxiv=192"]) == 0
    assert captured["source_overrides"] == {"arxiv": 192}
    capsys.readouterr()


def test_unknown_item_revive_fails_cleanly(cli_env, capsys):
    assert main(["revive", "arxiv:nope"]) == 1


def test_tune_train_drops_head_in_ckpt_dir(cli_env, tmp_path, capsys):
    """docs/03: tune exports, runs the CLM fine-tune, drops the head, suggests the new model name."""
    main(["run"])  # seed labelled examples
    capsys.readouterr()

    # seed ≥5 labelled arXiv items with verdicts (fit/train need real rows)
    store = Store(tmp_path / "cli.db")
    try:
        from sibilla.judge import configs as jcfgs
        from sibilla.store import VerdictKey, VerdictRow

        key = VerdictKey("cafe" * 16, jcfgs.get_config("arxiv").version, "fake", "fake-latest")
        for i in range(6):
            iid = f"arxiv:seed{i}"
            store.upsert_item(_seed_item(iid))
            store.save_verdict(
                VerdictRow(
                    item_id=iid,
                    key=key,
                    scores={"relevance": 8, "novelty": 0.5},
                    confidences={"relevance": 0.7, "novelty": 0.7},
                )
            )
            store.add_label(iid, "read", state_hash=key.state_hash)
    finally:
        store.close()

    ckpt = tmp_path / "ckpt"
    # a stub trainer: {dataset} → {head_out} is the whole "training run"
    (tmp_path / "sibilla.yaml").write_text(
        f"db_path: {tmp_path / 'cli.db'}\noutput_dir: {tmp_path}\n"
        f'judge:\n  finetune_cmd: "cp {{dataset}} {{head_out}}"\n  ckpt_dir: {ckpt}\n',
        encoding="utf-8",
    )
    head_out = tmp_path / "trained" / "arxiv-ft.pt"
    assert main(["tune", "--train", "--source", "arxiv", "--head-out", str(head_out)]) == 0
    out = capsys.readouterr().out
    assert (ckpt / "arxiv-ft.pt").exists()  # dropped where clm-serve hot-reloads
    assert "judge.model = arxiv-ft" in out  # the next cycle serves it under a new name

    # without finetune_cmd the loop degrades to instructions, never an error
    (tmp_path / "sibilla.yaml").write_text(
        f"db_path: {tmp_path / 'cli.db'}\noutput_dir: {tmp_path}\n", encoding="utf-8"
    )
    assert main(["tune", "--train", "--source", "arxiv"]) == 0
    assert "finetune_cmd not configured" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 1
