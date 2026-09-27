"""Store: idempotent items, verdict cache key, ledger, labels, calibrations."""

from __future__ import annotations

from sibilla.store import VerdictKey, VerdictRow

KEY = VerdictKey(state_hash="abc", judgeconfig_version="1", backend="fake", model="fake-latest")
KEY2 = VerdictKey(state_hash="def", judgeconfig_version="1", backend="fake", model="fake-latest")


def test_upsert_item_idempotent(store, sample_items):
    it = sample_items[0]
    assert store.upsert_item(it) is True
    assert store.upsert_item(it) is False  # same (source, native_id) → no dup
    rows = store.items_between(0, 2**31)
    assert len(rows) == 1
    assert rows[0].id == "arxiv:2609.00001"
    assert "sparse attention" in rows[0].body  # body landed in the bodies table


def test_verdict_cache_key(store, sample_items):
    store.upsert_item(sample_items[0])
    v = VerdictRow(item_id=sample_items[0].id, key=KEY, scores={"relevance": 8}, confidences={"relevance": 0.9})
    assert store.save_verdict(v) is True
    assert store.save_verdict(v) is False  # no item judged twice under the same key

    # same item, new state → judgeable again (re-judge only what changed underneath)
    v2 = VerdictRow(item_id=sample_items[0].id, key=KEY2, scores={"relevance": 7}, confidences={"relevance": 0.8})
    assert store.save_verdict(v2) is True
    assert len(store.verdicts_for_items([sample_items[0].id], KEY)) == 1
    assert len(store.verdicts_for_items([sample_items[0].id], KEY2)) == 1

    # backend switch → new key, old verdicts stay queryable for A/B (docs/03)
    key_other_backend = VerdictKey("abc", "1", "other", "fake-latest")
    assert store.items_without_verdict("arxiv", key_other_backend)  # pending again
    assert store.items_without_verdict("arxiv", KEY) == []


def test_items_without_verdict_scoped_by_source(store, sample_items):
    for it in sample_items:
        store.upsert_item(it)
    v = VerdictRow(item_id=sample_items[0].id, key=KEY, scores={}, confidences={})
    store.save_verdict(v)
    pending = store.items_without_verdict("arxiv", KEY)
    assert pending == []
    assert len(store.items_without_verdict("reddit", KEY)) == 1


def test_state_versions(store):
    store.save_state("h1", "state v1")
    store.save_state("h2", "state v2")
    assert store.latest_state()[0] == "h2"
    hashes = [h for h, _ in store.state_history()]
    assert hashes == ["h1", "h2"]


def test_cost_ledger(store):
    store.save_judge_call("typesafe", "jev", "rq1", 170, input_tokens=1_000_000, cost_usd=0.042)
    store.save_judge_call("clm", "clm-latest", "rq2", 1)
    store.save_judge_call("typesafe", "jev", "rq3", 10, error="boom")
    assert store.spend_between(0, 2**31) == 0.042  # errored calls don't bill
    assert store.call_errors_between(0, 2**31) == 1


def test_clusters_replace_and_lookup(store, sample_items):
    for it in sample_items:
        store.upsert_item(it)
    ids = store.replace_clusters([["arxiv:2609.00001", "hackernews:1"], ["reddit:abc"]])
    assert len(ids) == 1  # singletons are not clusters
    cid, members = store.cluster_of("hackernews:1")
    assert set(members) == {"arxiv:2609.00001", "hackernews:1"}
    assert store.cluster_counts(0, 2**31) == 1


def test_labels_unique(store, sample_items):
    store.upsert_item(sample_items[0])
    assert store.add_label(sample_items[0].id, "read", state_hash="abc")
    assert store.add_label(sample_items[0].id, "read", state_hash="abc") is False
    store.add_label(sample_items[0].id, "kill", state_hash="abc")
    assert len(store.labels(source="arxiv")) == 2


def test_calibration_roundtrip(store):
    store.save_calibration("clm", "clm-latest", "1", {"high": 0.9, "low": 0.4}, 12)
    assert store.calibration("clm", "clm-latest", "1") == {"high": 0.9, "low": 0.4}
    assert store.calibration("clm", "other", "1") is None  # head swap re-opens calibration
