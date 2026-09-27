"""Map build: DB rows → d3-hierarchy JSON → single-file HTML (docs/04).

The map is a pure function of the store. Layout geometry is computed at
view time by the vendored d3-hierarchy (treemap, squarify); this module
emits the node data, quadrant shares and all meta — no network calls, no
secrets in the output.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment

from sibilla.judge import configs as jcfgs
from sibilla.judge.base import Answer
from sibilla.store import ItemRow, VerdictRow

SOURCE_LABELS = {"arxiv": "arXiv", "hackernews": "HN", "reddit": "reddit", "x": "X"}
SOURCE_RICHNESS = {"arxiv": 0, "hackernews": 1, "reddit": 2, "x": 3}
DEFAULT_GATES = {"high": 0.8, "low": 0.5}  # docs/03 starting points, refit by `sibilla tune`
EXCERPT_CHARS = 220


@dataclass
class MapMeta:
    date_label: str
    state_hash: str = ""
    spend_usd: float = 0.0
    outages: list[str] = field(default_factory=list)  # "reddit: unreachable — ..."
    stale_state: bool = False
    backend_down: bool = False
    revived: int = 0
    window: tuple[int, int] = (0, 0)


def build_nodes(
    items: list[ItemRow],
    verdicts: dict[str, VerdictRow],
    cluster_members: dict[str, tuple[int, list[str]]],
    gates_for: dict[str, float] | Callable[[str], dict[str, float]] | None = None,
) -> list[dict[str, Any]]:
    """Place every judged item into its quadrant and emit renderable nodes.

    Clusters render as ONE node (docs/02+04): the richest body wins
    (canonical), scores are the max across members, confidence the min
    (gating must stay conservative), siblings become source badges + links.
    ``gates_for`` maps source → confidence gates (fitted per backend/model/
    judgeconfig_version, docs/03); a plain dict applies to every source.
    """
    resolve = gates_for if callable(gates_for) else (lambda _s: gates_for or DEFAULT_GATES)
    by_id = {i.id: i for i in items}

    # cluster pass: canonical = richest member; non-canonical members are
    # represented by it, never rendered separately
    canonical_of: dict[str, str] = {}
    for _item_id, (_, members) in cluster_members.items():
        present = [m for m in members if m in verdicts and m in by_id]
        if len(present) < 2:
            continue
        canonical = min(present, key=lambda m: (SOURCE_RICHNESS.get(m.split(":", 1)[0], 9), m))
        for m in present:
            canonical_of[m] = canonical

    nodes: list[dict[str, Any]] = []
    for item in items:
        if item.id in canonical_of and canonical_of[item.id] != item.id:
            continue  # the canonical cluster node speaks for this echo
        verdict = verdicts.get(item.id)
        if verdict is None:
            continue  # unjudged items never silently enter the map
        source = item.source
        sources: list[str] = [source]
        urls: dict[str, str | None] = {source: item.url} if item.url else {}
        echo = False
        boost = 1.0
        member_ids = [item.id]
        if item.id in cluster_members and canonical_of.get(item.id) == item.id:
            _, members = cluster_members[item.id]
            member_ids = [m for m in members if m in verdicts and m in by_id]
            sibling_sources = {m.split(":", 1)[0] for m in member_ids}
            if len(sibling_sources) >= 2:
                sources = sorted(sibling_sources)
                echo = True
                boost = _echo_boost(len(member_ids))
            for m in member_ids:
                if m != item.id and m not in urls:
                    urls[m.split(":", 1)[0]] = by_id[m].url  # evidence badges keep all links (docs/02)

        verdict_for_node = _merged_verdict(item, verdict, [verdicts[m] for m in member_ids], boost)
        answers = answers_from_verdict(source, verdict_for_node)
        quadrant, reason = jcfgs.classify(source, answers)
        confidence = jcfgs.min_confidence(answers)
        gates = resolve(source)
        if confidence < gates["low"]:
            quadrant = "HUMAN"  # low confidence must never silently kill (docs/03)
            reason = f"confidence {confidence:.2f} < {gates['low']:g} — you look here"
        size = jcfgs.size_score(source, answers)
        novelty = jcfgs.novelty(source, answers)
        nodes.append(
            {
                "id": item.id,
                "title": item.title or "(untitled)",
                "quadrant": quadrant,
                "size": round(max(size, 0.15), 3),  # zero-relevance still renders
                "novelty": round(novelty, 3),
                "confidence": round(confidence, 3),
                "composite": round(jcfgs.composite_score(source, answers), 2),
                "scores": dict(verdict_for_node.scores),
                "confidences": dict(verdict_for_node.confidences),
                "sources": sources,
                "urls": urls,
                "author": item.author or "",
                "excerpt": (item.body or "").strip().replace("\n", " ")[:EXCERPT_CHARS],
                "echo": echo,
                "truncated": verdict_for_node.truncated,
                "reason": reason,
                "fill": novelty_fill(novelty),
                "muted": False,
            }
        )
    return nodes


def _merged_verdict(item: ItemRow, own: VerdictRow, members: list[VerdictRow], boost: float) -> VerdictRow:
    """Cluster aggregate (docs/02): max(scores), min(confidence), any truncated.

    A single-member "cluster" (or the canonical alone) is just the own verdict.
    """
    if len(members) <= 1:
        return own
    scores: dict[str, Any] = {}
    confidences: dict[str, Any] = {}
    for mv in members:
        for k, v in mv.scores.items():
            if isinstance(v, int | float) and not isinstance(v, bool):
                scores[k] = max(scores.get(k, v), v) if k in scores else v
        for k, v in mv.confidences.items():
            confidences[k] = min(confidences.get(k, v), v) if k in confidences else v
    merged = VerdictRow(
        item_id=item.id,
        key=own.key,
        scores=scores,
        confidences=confidences,
        truncated=any(m.truncated for m in members),
        created_at=max(m.created_at for m in members),
    )
    # boost applies to the treemap area driver, after max()
    if "relevance" in merged.scores and isinstance(merged.scores["relevance"], int | float):
        merged.scores["relevance"] = merged.scores["relevance"] * boost
    return merged


def answers_from_verdict(source: str, verdict: VerdictRow) -> dict[str, Answer]:
    """Rebuild typed answers from stored JSON so classify() stays pure."""
    questions = {q.id: q for q in jcfgs.get_config(source).questions}
    out: dict[str, Answer] = {}
    for qid, value in verdict.scores.items():
        q = questions.get(qid)
        if q is None:
            continue  # composite and other derived keys are not questions
        conf = float(verdict.confidences.get(qid, 1.0))
        out[qid] = Answer(qid, q.primitive, value, conf)
    return out


def _echo_boost(n_members: int) -> float:
    if n_members < 2:
        return 1.0
    return min(1.3, 1.0 + 0.1 * (n_members - 1))


def novelty_fill(novelty: float) -> str:
    """Cool = known ground → hot = new-to-you (hue 210° → 10°)."""
    hue = 210 - 200 * max(0.0, min(1.0, novelty))
    return f"hsl({hue:.0f}, 70%, 55%)"


def build_map_data(meta: MapMeta, nodes: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {"READ": 0, "SKIM": 0, "KILL": 0, "HUMAN": 0}
    for n in nodes:
        counts[n["quadrant"]] += 1
    for n in nodes:
        if n["quadrant"] == "KILL":
            n["fill"] = _desaturate(n["fill"])  # SKIM muting is a CSS cell class, not per-node
    banners = list(meta.outages)
    if meta.stale_state:
        banners.append("state rebuild failed — using last good state")
    if meta.backend_down:
        banners.append("judge backend down — map renders last verdicts (stale)")
    return {
        "meta": {
            "title": f"SIBILLA — {meta.date_label}",
            "state": meta.state_hash[:6],
            "sources": sorted({s for n in nodes for s in n["sources"]}),
            "items": len(nodes),
            "clusters": sum(1 for n in nodes if n["echo"]),
            "spend": f"${meta.spend_usd:.3f}",
            "counts": counts,
            "banners": banners,
            "revived": meta.revived,
            "generated_at": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z"),
        },
        "nodes": nodes,
    }


def _desaturate(fill: str) -> str:
    """KILL uses the same encoding, heavily muted (visual noise stays low)."""
    try:
        hue, sat, _light = fill.removeprefix("hsl(").removesuffix(")").split(",")
        return f"hsl({hue.strip()},{max(10, int(sat.strip().rstrip('%')) // 4)}%,78%)"
    except ValueError:
        return fill


def render_html(data: dict[str, Any], gates: dict[str, float] | None = None) -> str:
    """Assemble the single-file HTML (inlined d3 + data, no external refs)."""
    env = Environment(autoescape=False, keep_trailing_newline=True)
    template = env.from_string(_TEMPLATE_SOURCE())
    # Escape <> in the embedded JSON: a title containing "</script>" must not
    # terminate the data script block early (the classic inline-JSON break).
    data_json = (
        json.dumps(data, ensure_ascii=False).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    )
    return template.render(
        data_json=data_json,
        d3_library=_D3_SOURCE(),
        gates_json=json.dumps(gates or DEFAULT_GATES),
    )


def _TEMPLATE_SOURCE() -> str:
    return (Path(__file__).parent / "template.html.j2").read_text(encoding="utf-8")


def _D3_SOURCE() -> str:
    return (Path(__file__).parent / "vendor" / "d3-hierarchy.min.js").read_text(encoding="utf-8")


def quadrant_summary(nodes: list[dict[str, Any]]) -> str:
    counts = {"READ": 0, "SKIM": 0, "KILL": 0, "HUMAN": 0}
    for n in nodes:
        counts[n["quadrant"]] += 1
    return f"{counts['READ']} READ · {counts['SKIM']} SKIM · {counts['KILL'] + counts['HUMAN']} kill"


def delivery_summary(data: dict[str, Any]) -> str:
    """One-line summary for the Telegram home-channel message (docs/04)."""
    m = data["meta"]
    return (
        f"Sibilla · {m['title'].split('—')[-1].strip()} ·"
        f" {m['counts']['READ']} READ · {m['counts']['SKIM']} SKIM · {m['counts']['KILL']} kill"
    )
