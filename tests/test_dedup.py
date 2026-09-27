"""Dedup: URL normalization, deterministic keys, union-find, semantic residual."""

from __future__ import annotations

from sibilla.dedup import cluster as dedup


def _item(id_, source, url=None, body="", title="", meta=None):
    from sibilla.store import ItemRow

    return ItemRow(
        id=id_,
        source=source,
        native_id=id_.split(":", 1)[-1],
        fetched_at=0,
        title=title,
        url=url,
        published=0,
        meta=meta or {},
        body=body,
    )


def test_normalize_url_strips_utm_www_scheme_trailing_slash():
    a = dedup.normalize_url("https://example.com/post/?utm_source=x&id=7")
    b = dedup.normalize_url("http://www.example.com/post?id=7#frag")
    assert a == b == "example.com/post?id=7"


def test_dedup_keys_arxiv_id_and_meta():
    paper = _item(
        "arxiv:2609.12345v1", "arxiv", url="https://arxiv.org/abs/2609.12345v1", meta={"arxiv_base_id": "2609.12345"}
    )
    keys = dedup.dedup_keys(paper)
    assert "arxiv:2609.12345" in keys  # version-stripped: v1 and v2 echo
    assert any(k.startswith("url:arxiv.org/abs/2609.12345") for k in keys)


def test_dedup_keys_github_repo_from_title():
    post = _item("hn:1", "hackernews", title="Show HN: github.com/maeste/radar-core is out")
    assert "repo:maeste/radar-core" in dedup.dedup_keys(post)


def test_cluster_deterministic_cross_source_echo(sample_items):
    clusters = dedup.cluster_deterministic(sample_items)
    multi = [c for c in clusters if len(c.members) >= 2]
    assert len(multi) == 1
    members = multi[0].members
    # canonical is the richest body: paper > thread > tweet (docs/02)
    assert members[0] == "arxiv:2609.00001"
    assert set(members) == {"arxiv:2609.00001", "hackernews:1"}


def test_no_cluster_without_shared_keys(sample_items):
    for it in sample_items:
        it.url = None
        it.meta = {}
        it.title = f"distinct title {it.id}"
        it.body = "nothing in common"
    clusters = dedup.cluster_deterministic(sample_items)
    assert all(len(c.members) == 1 for c in clusters)


def test_echo_boost_deterministic():
    assert dedup.echo_boost_multiplier(1) == 1.0
    assert dedup.echo_boost_multiplier(2) == pytest_approx(1.1)
    assert dedup.echo_boost_multiplier(4) == pytest_approx(1.3)  # capped


def pytest_approx(x):
    from pytest import approx

    return approx(x)


def test_semantic_residual_joins_via_rank(fake_backend, sample_items):
    # arXiv+HN already clustered by the deterministic pass
    base = dedup.cluster_deterministic(sample_items[:2])
    reddit = sample_items[2]
    reddit.title = "same-marker sparse attention on reddit"
    quadrant_by_item = {reddit.id: "READ"}
    clusters = dedup.cluster_semantic_residual(
        [reddit, *sample_items[:2]], base, fake_backend, quadrant_by_item, threshold=0.75
    )
    joined = next(c for c in clusters if reddit.id in c.members)
    assert set(joined.members) == {"arxiv:2609.00001", "hackernews:1", "reddit:abc"}


def test_semantic_residual_ignores_kill(fake_backend, sample_items):
    base = dedup.cluster_deterministic(sample_items[:2])
    reddit = sample_items[2]
    reddit.title = "same-marker sparse attention on reddit"
    quadrant_by_item = {reddit.id: "KILL"}  # only READ/SKIM strays are candidates (docs/02)
    clusters = dedup.cluster_semantic_residual([reddit, *sample_items[:2]], base, fake_backend, quadrant_by_item)
    assert all(reddit.id not in c.members or c.members == [reddit.id] for c in clusters)
