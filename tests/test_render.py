"""Render: placement + confidence gating + the single-file HTML contract."""

from __future__ import annotations

from sibilla.render import treemap
from sibilla.render.treemap import MapMeta, build_map_data, build_nodes, render_html
from sibilla.store import VerdictKey, VerdictRow

KEY = VerdictKey("abc", "1", "fake", "fake-latest")


def _verdict(item_id, relevance=8.0, novelty=0.9, conf=0.95, applicability=7.0):
    scores = {"relevance": relevance, "novelty": novelty, "applicability": applicability}
    confs = {k: conf for k in scores}
    return VerdictRow(item_id=item_id, key=KEY, scores=scores, confidences=confs)


def test_placement_read_skim_kill_and_gates(sample_items):
    verdicts = {
        "arxiv:2609.00001": _verdict("arxiv:2609.00001", relevance=8.0),
        "hackernews:1": VerdictRow(
            item_id="hackernews:1",
            key=KEY,
            scores={"relevance": 5.0, "substance": 0.9, "kind": "announcement"},
            confidences={"relevance": 0.95, "substance": 0.95, "kind": 0.95},
        ),
        "reddit:abc": VerdictRow(
            item_id="reddit:abc",
            key=KEY,
            scores={"relevance": 2.0, "novelty": 0.5, "kind": "experience-report"},
            confidences={"relevance": 0.95, "novelty": 0.95, "kind": 0.95},
        ),
    }
    nodes = build_nodes(sample_items, verdicts, {})
    quads = {n["id"]: n["quadrant"] for n in nodes}
    assert quads["arxiv:2609.00001"] == "READ"  # 0.7*8 + 0.3*7 = 7.7 ≥ 7
    assert quads["hackernews:1"] == "SKIM"
    assert quads["reddit:abc"] == "KILL"


def test_low_confidence_never_silently_kills(sample_items):
    """confidence < 0.5 → human zone, even with a KILL composite (docs/03)."""
    verdicts = {"reddit:abc": _verdict("reddit:abc", relevance=1.0, conf=0.3)}
    nodes = build_nodes(sample_items, verdicts, {})
    assert nodes[0]["quadrant"] == "HUMAN"
    assert "you look here" in nodes[0]["reason"]


def test_dashed_border_band_gating(sample_items):
    verdicts = {"arxiv:2609.00001": _verdict("arxiv:2609.00001", conf=0.65)}
    nodes = build_nodes(sample_items, verdicts, {})
    node = nodes[0]
    assert node["quadrant"] == "READ"  # placed normally...
    assert 0.5 <= node["confidence"] < 0.8  # ...and the HTML draws it dashed


def test_echo_cluster_is_one_node_richest_wins_max_scores(sample_items):
    """docs/02+04: a cluster renders as ONE node — richest body, max scores,
    siblings as badges with links, deterministic convergence boost."""
    members = ("x", ["arxiv:2609.00001", "hackernews:1"])
    verdicts = {
        "arxiv:2609.00001": _verdict("arxiv:2609.00001", relevance=8.0, applicability=7.0),
        # sibling scored higher: the cluster inherits max(scores)
        "hackernews:1": VerdictRow(
            item_id="hackernews:1",
            key=KEY,
            scores={"relevance": 9.0, "substance": 0.9, "kind": "announcement"},
            confidences={"relevance": 0.95, "substance": 0.95, "kind": 0.95},
        ),
    }
    nodes = build_nodes(sample_items[:2], verdicts, {"arxiv:2609.00001": members, "hackernews:1": members})
    assert len(nodes) == 1, "the echo collapses into a single canonical node"
    node = nodes[0]
    assert node["id"] == "arxiv:2609.00001"  # paper > thread: richest body wins
    assert set(node["sources"]) == {"arxiv", "hackernews"}
    assert node["echo"] is True
    assert node["urls"]["hackernews"] is not None  # evidence badges keep all links
    assert node["size"] == pytest_approx(9.9)  # max(8, 9) × 1.1 boost
    assert node["scores"]["relevance"] == pytest_approx(9.9)


def test_cluster_min_confidence_gates_conservatively(sample_items):
    """One uncertain member drags the whole cluster to the human zone —
    low confidence must never silently kill (docs/03)."""
    members = ("x", ["arxiv:2609.00001", "hackernews:1"])
    verdicts = {
        "arxiv:2609.00001": _verdict("arxiv:2609.00001", conf=0.95),
        "hackernews:1": VerdictRow(
            item_id="hackernews:1",
            key=KEY,
            scores={"relevance": 9.0, "substance": 0.9, "kind": "announcement"},
            confidences={"relevance": 0.3, "substance": 0.3, "kind": 0.3},
        ),
    }
    nodes = build_nodes(sample_items[:2], verdicts, {"arxiv:2609.00001": members, "hackernews:1": members})
    assert len(nodes) == 1
    assert nodes[0]["quadrant"] == "HUMAN"
    assert nodes[0]["confidence"] == pytest_approx(0.3)


def pytest_approx(x):
    from pytest import approx

    return approx(x)


def test_novelty_fill_cool_to_hot():
    assert treemap.novelty_fill(0.0).startswith("hsl(210")
    assert treemap.novelty_fill(1.0).startswith("hsl(10")


def test_build_map_data_counts_and_kill_desaturation(sample_items):
    verdicts = {
        "arxiv:2609.00001": _verdict("arxiv:2609.00001"),
        "hackernews:1": VerdictRow(
            item_id="hackernews:1",
            key=KEY,
            scores={"relevance": 3.0, "substance": 0.9},
            confidences={"relevance": 0.95, "substance": 0.95},
        ),
        "reddit:abc": _verdict("reddit:abc", relevance=1.0, conf=0.3),
    }
    meta = MapMeta(
        date_label="2026-09-27",
        state_hash="abc123",
        spend_usd=0.031,
        outages=["reddit: unreachable — map covers 2/3 sources"],
        window=(0, 1),
    )
    nodes = build_nodes(sample_items, verdicts, {})
    data = build_map_data(meta, nodes)
    m = data["meta"]
    assert m["counts"] == {"READ": 1, "SKIM": 0, "KILL": 1, "HUMAN": 1}
    assert m["spend"] == "$0.031"
    assert any("reddit" in b for b in m["banners"])
    kill_node = next(n for n in data["nodes"] if n["quadrant"] == "KILL")
    assert "78%" in kill_node["fill"]  # heavily muted encoding


def test_render_html_is_self_contained(sample_items):
    meta = MapMeta(date_label="2026-09-27", state_hash="abc123", window=(0, 1))
    verdict = VerdictRow(
        item_id="arxiv:2609.00001",
        key=KEY,
        scores={"relevance": 8.0, "novelty": 0.9, "applicability": 7.0},
        confidences={"relevance": 0.95, "novelty": 0.95, "applicability": 0.95},
    )
    nodes = build_nodes(sample_items[:1], {"arxiv:2609.00001": verdict}, {})
    html = render_html(build_map_data(meta, nodes))
    # single-file contract: no network at view time (docs/01 rule 5)
    assert 'src="http' not in html
    assert 'href="http' not in html
    assert "d3js.org/d3-hierarchy" in html  # d3 vendored, not CDN-linked
    assert "treemapSquarify" in html and "HUMAN ZONE" in html
    # docs/04: KILL filterable by source; novelty duplicated in a border pattern
    assert "killfilter" in html and "novel-band" in html and "killtoggle" in html
    # no secrets in the output (docs/01): the state text never enters the HTML
    assert "ACTIVE WORK PROFILE" not in html
    # accessibility: novelty duplicated outside color (glyph), confidence via dashed CSS
    assert "lowconf" in html and "glyph" in html


def test_render_html_escapes_titles():
    """Titles are data, not markup — a hostile title cannot break out of the data block."""
    from sibilla.store import ItemRow

    item = ItemRow(
        id="hn:9",
        source="hackernews",
        native_id="9",
        fetched_at=0,
        title="<script>alert(1)</script> cross-site",
        published=0,
    )
    verdict = VerdictRow(
        item_id="hn:9",
        key=KEY,
        scores={"relevance": 7, "substance": 0.9, "kind": "announcement"},
        confidences={"relevance": 0.9, "substance": 0.9, "kind": 0.9},
    )
    meta = MapMeta(date_label="2026-09-27", window=(0, 1))
    nodes = build_nodes([item], {"hn:9": verdict}, {})
    html = render_html(build_map_data(meta, nodes))
    assert html.count("<script>") == 2  # d3 + data scripts only
    assert "<script>alert(1)" not in html  # embedded JSON has <> escaped
    assert "cross-site" in html  # the data itself survives, inert


def test_unjudged_items_never_enter_the_map(sample_items):
    nodes = build_nodes(sample_items, {}, {})  # nothing judged
    assert nodes == []


def test_empty_map_says_why(sample_items):
    """docs/04 honesty: fetched-but-unjudged ≠ quiet day. The data carries the
    pending count; the template's quiet card names the cause and the fix."""
    import json
    import re

    def embedded_meta(html: str) -> dict:
        return json.loads(re.search(r"^const DATA = (.*?);$", html, re.M).group(1))["meta"]

    meta_judged = MapMeta(date_label="2026-09-27", pending=0)
    meta_pending = MapMeta(date_label="2026-09-27", pending=2)

    quiet = render_html(build_map_data(meta_judged, []))
    silent = render_html(build_map_data(meta_pending, []))

    assert embedded_meta(quiet)["pending"] == 0
    assert embedded_meta(silent)["pending"] == 2
    # both card variants are in the template; the pending one names cause and fix
    assert "A quiet day." in quiet
    assert "The oracle is silent." in silent
    assert "unjudged" in silent and "sibilla run" in silent
    assert "nothing is lost" in silent  # the store keeps fetched items — no re-fetch anxiety
