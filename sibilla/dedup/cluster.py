"""The echo engine (docs/02).

1. Deterministic keys first: normalized URL (strip UTM), arXiv ID regex,
   GitHub repo full name. Catches most echoes at zero cost.
2. Semantic residual (v2, requires a healthy backend): for READ/SKIM items
   still unclustered, ``judge.rank()`` asks "is this the same underlying
   story?" against existing cluster canonicals — never O(n²).
3. The cluster node inherits max(scores) and the richest body
   (paper > thread > tweet) — see render/map build.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlparse

from sibilla.judge.base import JudgeBackend
from sibilla.store import ItemRow

UTM_PREFIXES = ("utm_", "ref_", "fbclid", "gclid", "mc_cid", "mc_eid")
ARXIV_ID_RE = re.compile(r"(?:arxiv\.org/(?:abs|pdf)/|(?<![\w.]))(\d{4}\.\d{4,5})(?:v\d+)?", re.IGNORECASE)
GITHUB_REPO_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)", re.IGNORECASE)

# richest body wins (docs/02): paper > thread > tweet
SOURCE_RICHNESS = {"arxiv": 0, "hackernews": 1, "reddit": 2, "x": 3}


@dataclass
class Cluster:
    members: list[str]  # item ids, richest first

    @property
    def canonical(self) -> str:
        return self.members[0]


def normalize_url(url: str | None) -> str | None:
    """Strip scheme, www, UTM params, fragments and trailing slash."""
    if not url:
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if not parsed.netloc:
        return None
    host = parsed.netloc.lower().removeprefix("www.")
    query = urlencode(
        [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True) if not k.lower().startswith(UTM_PREFIXES)]
    )
    path = parsed.path.rstrip("/")
    return f"{host}{path}{'?' + query if query else ''}"


def dedup_keys(item: ItemRow) -> set[str]:
    """All deterministic keys an item carries (empty set → never clusters)."""
    keys: set[str] = set()
    norm = normalize_url(item.url)
    if norm:
        keys.add(f"url:{norm}")
    for source_text in (item.url or "", item.title or "", item.body[:2000]):
        for match in ARXIV_ID_RE.finditer(source_text):
            keys.add(f"arxiv:{match.group(1).lower()}")
        for match in GITHUB_REPO_RE.finditer(source_text):
            repo = match.group(1).lower()
            if repo.rstrip("/").count("/") == 1:
                keys.add(f"repo:{repo.rstrip('/')}")
    meta = item.meta or {}
    base_id = meta.get("arxiv_base_id")
    if base_id:
        keys.add(f"arxiv:{str(base_id).lower()}")
    return keys


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, x: str) -> str:
        self.parent.setdefault(x, x)
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[x] != root:  # path compression
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _richness(item: ItemRow) -> tuple[int, str]:
    return (SOURCE_RICHNESS.get(item.source, 9), item.id)


def cluster_deterministic(items: list[ItemRow]) -> list[Cluster]:
    """Pass 1: union-find over shared deterministic keys (zero judge cost)."""
    uf = UnionFind()
    by_id = {i.id: i for i in items}
    key_index: dict[str, str] = {}
    for item in sorted(items, key=_richness):
        uf.find(item.id)
        for key in dedup_keys(item):
            if key in key_index:
                uf.union(key_index[key], item.id)
            else:
                key_index[key] = item.id
    groups: dict[str, list[str]] = defaultdict(list)
    for item_id in by_id:
        groups[uf.find(item_id)].append(item_id)
    clusters = []
    for members in groups.values():
        members.sort(key=lambda i: _richness(by_id[i]))
        clusters.append(Cluster(members=members))
    return clusters


def cluster_semantic_residual(
    items: list[ItemRow],
    base: list[Cluster],
    backend: JudgeBackend,
    quadrant_by_item: dict[str, str],
    threshold: float = 0.75,
    max_canonicals: int = 50,
) -> list[Cluster]:
    """Pass 2 (v2): rank() joins READ/SKIM strays to existing cluster canonicals.

    Context = canonical item title + first sentences; question = "Is this the
    same underlying story?"; prob ≥ threshold ⇒ same cluster. Candidates are
    bounded: canonicals capped at ``max_canonicals`` (most recently judged),
    each stray judged once.
    """
    by_id = {i.id: i for i in items}
    clusters = [Cluster(members=list(c.members)) for c in base]
    clustered_ids = {m for c in clusters for m in c.members}
    strays = [
        by_id[i]
        for i, q in quadrant_by_item.items()
        if i in by_id and q in ("READ", "SKIM") and i not in clustered_ids
    ]
    if not strays or not clusters:
        return clusters

    question = "Is this the same underlying story?"

    def context_of(item: ItemRow) -> str:
        return f"{item.title or ''}\n{(item.body or '')[:400]}".strip()

    for stray in sorted(strays, key=_richness):
        canonicals = [by_id[c.canonical] for c in clusters if c.canonical in by_id][-max_canonicals:]
        if not canonicals:
            break
        ranked = backend.rank(context_of(stray), question, [context_of(c) for c in canonicals])
        best_idx = max(range(len(ranked.probabilities)), key=lambda k: ranked.probabilities[k], default=None)
        if best_idx is not None and ranked.probabilities[best_idx] >= threshold:
            clusters[best_idx].members.append(stray.id)
            clusters[best_idx].members.sort(key=lambda i: _richness(by_id[i]))
    return clusters


def echo_boost_multiplier(n_sources: int) -> float:
    """Deterministic area boost for convergence (docs/04): +10% per extra source, cap 1.3."""
    if n_sources < 2:
        return 1.0
    return min(1.3, 1.0 + 0.1 * (n_sources - 1))


def assign_clusters(item_ids: list[str], clusters: list[Cluster]) -> dict[str, tuple[int, list[str]]]:
    """Map item_id → (cluster_size, members) for nodes that are part of a multi-source echo."""
    by_id = set(item_ids)
    out: dict[str, tuple[int, list[str]]] = {}
    for c in clusters:
        if len(c.members) < 2:
            continue
        members = [m for m in c.members if m in by_id]
        if len(members) < 2:
            continue
        for m in members:
            out[m] = (len(members), members)
    return out
