"""Pipeline orchestration: fetch → normalize → state → judge → map build (docs/01).

Each stage is independently testable; failures degrade per docs/01's failure
table — a source outage or a backend outage shrinks the map with a loud
banner, never silently. The map is a pure function of the store.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sibilla.config import Config
from sibilla.dedup import cluster as dedup
from sibilla.delivery import deliver_telegram
from sibilla.judge import configs as jcfgs
from sibilla.judge.base import JudgeBackend, JudgeUnavailable
from sibilla.judge.clm import ClmBackend
from sibilla.judge.packer import JudgeJob, Packer
from sibilla.judge.typesafe import TypesafeBackend
from sibilla.render import treemap
from sibilla.sources.base import RawItem, SourceFetchError, domain_of, item_text, sources_registry
from sibilla.state import builder as state_builder
from sibilla.store import ItemRow, Store, VerdictKey, VerdictRow

STATE_MAX_AGE_S = 30 * 24 * 3600  # monthly refresh cadence (docs/03)


@dataclass
class RunReport:
    date_label: str
    window_hours: int | None = None  # None → config default (24h); 168 = weekly recap
    source_overrides: dict[str, int] = field(default_factory=dict)  # --source-window name=h
    fetched: dict[str, int] = field(default_factory=dict)
    judged: int = 0
    judge_errors: int = 0
    clusters: int = 0
    spend_usd: float = 0.0
    map_path: str | None = None
    delivery: str | None = None
    outages: list[str] = field(default_factory=list)
    stale_state: bool = False
    backend_down: bool = False
    state_hash: str = ""

    def summary(self) -> str:
        bits = [f"{src}: {n}" for src, n in self.fetched.items()]
        parts = [
            f"sibilla run {self.date_label}",
            f"fetched [{', '.join(bits) or 'nothing new'}]",
            f"judged {self.judged} (+{self.judge_errors} errors)",
            f"{self.clusters} clusters",
            f"${self.spend_usd:.3f}",
        ]
        if self.stale_state:
            parts.append("STATE STALE (last good used)")
        if self.backend_down:
            parts.append("BACKEND DOWN (last verdicts)")
        for o in self.outages:
            parts.append(f"OUTAGE {o}")
        if self.map_path:
            parts.append(f"map → {self.map_path}")
        if self.delivery:
            parts.append(f"delivery: {self.delivery}")
        return " | ".join(parts)


def make_backend(cfg: Config) -> JudgeBackend:
    """Build the configured backend; a request written for one replays on the other."""
    j = cfg.judge
    cls = ClmBackend if j.backend == "clm" else TypesafeBackend
    api_key = os.environ.get(j.api_key_env) if j.api_key_env else None
    backend = cls(
        base_url=j.base_url or cls.default_base_url,
        api_key=api_key or None,
        model=j.model,
        timeout_s=j.timeout_s,
        retries=j.retries,
    )
    if j.input_cost_per_mtok is not None:
        backend.input_cost_per_mtok = j.input_cost_per_mtok
    return backend


def window_for(
    cfg: Config, date_label: str | None = None, window_hours: int | None = None
) -> tuple[datetime, datetime, str]:
    """The window a daily map covers: ``window_hours`` ending at the delivery hour.

    ``window_hours=168`` gives the weekly recap (docs/04 daily flow). The
    start is the global default; per-source windows narrow/extend fetch and
    map coverage per source (see ``source_window_hours``).
    """
    now = datetime.now().astimezone()
    if date_label:
        base = datetime.strptime(date_label, "%Y-%m-%d").astimezone()
        end = base.replace(hour=cfg.delivery.hour, minute=0, second=0, microsecond=0)
        if end > now:  # a future cutoff would silently shrink the world
            end = now
    else:
        end = now
    start = end - timedelta(hours=window_hours or cfg.window_hours)
    return start, end, end.strftime("%Y-%m-%d")


def source_window_hours(
    cfg: Config,
    name: str,
    window_hours: int | None = None,
    source_overrides: dict[str, int] | None = None,
) -> int:
    """Effective window (hours) for one source — arXiv needs more than HN.

    Precedence: ``--source-window name=h`` (CLI, per source) > ``--window-hours``
    (CLI, global) > per-source ``window:`` in sibilla.yaml > global ``window:``.
    arXiv lists with a lag and goes quiet over weekends, so it wants a wider
    window than real-time sources; wider is always safe because the store
    dedups by native id and the verdict cache makes re-fetches free.
    """
    if source_overrides and name in source_overrides:
        return int(source_overrides[name])
    if window_hours:
        return int(window_hours)
    src_cfg = getattr(cfg.sources, name, None)
    per_source = getattr(src_cfg, "window_hours", None)
    if per_source:
        return int(per_source)
    return cfg.window_hours


def widest_window_start(
    cfg: Config,
    end: datetime,
    window_hours: int | None = None,
    source_overrides: dict[str, int] | None = None,
) -> int:
    """Earliest ts covered by the union of the per-source windows (clusters, spend, revived)."""
    names = jcfgs.known_sources()
    widest = max(
        (source_window_hours(cfg, s, window_hours, source_overrides) for s in names),
        default=cfg.window_hours,
    )
    return int((end - timedelta(hours=widest)).timestamp())


def fetch_and_normalize(
    cfg: Config,
    store: Store,
    end: datetime,
    window_hours: int | None = None,
    source_overrides: dict[str, int] | None = None,
) -> tuple[dict[str, int], list[str]]:
    """Stages 1+2: pull raw items into the store, keyed by source-native ID.

    Each source fetches with its own window (arXiv 96h in the example config
    vs 24h for real-time sources): idempotent by (source, native_id), so a
    wide window only backfills, it never re-judges. Sources are isolated:
    one failing source records an outage note and the pipeline continues
    with the rest (docs/01 failure table).
    """
    registry = sources_registry()
    kwargs: dict[str, dict[str, Any]] = {
        "arxiv": {"cfg": cfg.sources.arxiv},
        "hackernews": {"cfg": cfg.sources.hackernews},
        "reddit": {"cfg": cfg.sources.reddit},
        "x": {"cfg": cfg.sources.x},
    }
    counts: dict[str, int] = {}
    outages: list[str] = []
    for name in ("arxiv", "hackernews", "reddit", "x"):
        if not cfg.sources.is_enabled(name):
            continue
        since = end - timedelta(hours=source_window_hours(cfg, name, window_hours, source_overrides))
        try:
            raw_items: list[RawItem] = registry[name](**kwargs[name]).fetch(since)
        except SourceFetchError as exc:
            outages.append(f"{name}: unreachable — {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — a plugin bug must not take down the cycle
            outages.append(f"{name}: fetch failed — {type(exc).__name__}: {exc}")
            continue
        n = 0
        for raw in raw_items:
            row = ItemRow(
                id=f"{raw.source}:{raw.native_id}",
                source=raw.source,
                native_id=raw.native_id,
                fetched_at=int(time.time()),
                title=raw.title,
                url=raw.url,
                author=raw.author,
                published=int(raw.published.timestamp()) if raw.published else None,
                meta=dict(raw.meta),
                body=raw.body,
            )
            if store.upsert_item(row):
                n += 1
        counts[name] = n
    return counts, outages


def ensure_state(cfg: Config, store: Store, rebuild: bool = False) -> tuple[str, str, bool]:
    """Stage 3: build or reuse the state; fall back to last good on gh failure."""
    latest = store.latest_state()
    if latest and not rebuild and (time.time() - latest[2]) < STATE_MAX_AGE_S:
        return latest[0], latest[1], False
    try:
        token = os.environ.get("GITHUB_TOKEN") or None
        state = state_builder.build_state(cfg.state, token=token)
        store.save_state(state.state_hash, state.text)
        return state.state_hash, state.text, False
    except state_builder.StateBuilderError:
        if latest is None:
            raise
        return latest[0], latest[1], True


def judge_pending(
    cfg: Config, store: Store, backend: JudgeBackend, state_hash: str, state_text: str
) -> tuple[int, int]:
    """Stage 4: judge every item missing a verdict for the current cache key.

    Requests never mix JudgeConfigs (one job per source); truncation and
    per-item failure isolation live in the packer (docs/03).
    """
    model = getattr(backend, "model", cfg.judge.model)
    packer = Packer(
        backend,
        ledger=store.save_judge_call,
        concurrency=cfg.judge.concurrency,
        pack_size=cfg.judge.pack_size,
        temperature=cfg.judge.temperature,
    )
    judged = errors = 0
    for source in jcfgs.known_sources():
        if not cfg.sources.is_enabled(source):
            continue
        jc = jcfgs.get_config(source)
        job_key = VerdictKey(state_hash, jc.version, backend.name, model)
        pending = store.items_without_verdict(source, job_key)
        if not pending:
            continue
        job = JudgeJob(source=source, config=jc)
        for item in pending:
            job.add(item.id, item_text(item.title, item.body, domain_of(item.url)))
        result = packer.run(state_text, [job])
        now = int(time.time())
        for item_id, answers in result.answers_by_item.items():
            verdict = VerdictRow(
                item_id=item_id,
                key=job_key,
                scores=jcfgs.answers_to_scores(source, answers),
                confidences=jcfgs.answers_to_confidences(answers),
                truncated=result.truncated_by_item.get(item_id, False),
                created_at=now,
            )
            if store.save_verdict(verdict):
                judged += 1
        errors += len(result.errors_by_item)  # ledgered per item; the map renders what succeeded
    return judged, errors


def build_clusters(cfg: Config, store: Store, backend: JudgeBackend | None, start_ts: int, end_ts: int) -> int:
    """Stage 5a: echo detection — deterministic keys first, semantic residual second."""
    items = store.items_between(start_ts, end_ts)
    if not items:
        store.replace_clusters([])
        return 0
    clusters = dedup.cluster_deterministic(items)
    if cfg.judge.semantic_dedup and backend is not None:
        model = getattr(backend, "model", cfg.judge.model)
        state_hash = (store.latest_state() or ("", "", 0))[0]
        verdicts: dict[str, VerdictRow] = {}
        for source in jcfgs.known_sources():
            vkey = VerdictKey(state_hash, jcfgs.get_config(source).version, backend.name, model)
            verdicts.update(store.verdicts_for_items([i.id for i in items if i.source == source], vkey))
        quadrant_by_item = {
            item.id: jcfgs.classify(item.source, treemap.answers_from_verdict(item.source, v))[0]
            for item, v in ((i, verdicts.get(i.id)) for i in items)
            if v is not None
        }
        clusters = dedup.cluster_semantic_residual(
            items, clusters, backend, quadrant_by_item, threshold=cfg.judge.rank_threshold
        )
    member_lists = [c.members for c in clusters]
    store.replace_clusters(member_lists)
    return sum(1 for m in member_lists if len(m) >= 2)


def latest_verdict(store: Store, item_id: str, backend_name: str, model: str) -> VerdictRow | None:
    """Newest verdict for the item under this backend+model (any state/config version).

    The map shows the current judgment; older verdicts stay queryable for A/B.
    """
    row = store.conn.execute(
        "SELECT * FROM verdicts WHERE item_id = ? AND backend = ? AND model = ?"
        " ORDER BY created_at DESC, id DESC LIMIT 1",
        (item_id, backend_name, model),
    ).fetchone()
    if row is None:
        return None
    return VerdictRow(
        item_id=item_id,
        key=VerdictKey(row["state_hash"], row["judgeconfig_version"], row["backend"], row["model"]),
        scores=json.loads(row["scores"] or "{}"),
        confidences=json.loads(row["confidences"] or "{}"),
        truncated=bool(row["truncated"]),
        created_at=row["created_at"] or 0,
        id=row["id"],
    )


def build_map(cfg: Config, store: Store, backend: JudgeBackend, report: RunReport) -> Path | None:
    """Stage 5b: the map, a pure function of the DB; optionally delivers.

    Each source contributes the items published inside its own window
    (arXiv 96h in the example config vs 24h real-time sources) — the map
    shows what this cycle's fetch actually covered, per source.
    """
    _, end, label = window_for(cfg, report.date_label, report.window_hours)
    end_ts = int(end.timestamp())
    start_ts = widest_window_start(cfg, end, report.window_hours, report.source_overrides)
    model = getattr(backend, "model", cfg.judge.model)
    latest = store.latest_state()

    items: list[ItemRow] = []
    for name in jcfgs.known_sources():  # all known: yesterday's fetches still render
        w_start = end_ts - source_window_hours(cfg, name, report.window_hours, report.source_overrides) * 3600
        items.extend(store.items_between(w_start, end_ts, sources=[name]))
    items.sort(key=lambda i: i.published or 0, reverse=True)
    pairs = [(item, latest_verdict(store, item.id, backend.name, model)) for item in items]
    judged_items = [i for i, v in pairs if v is not None]
    verdict_map = {i.id: v for i, v in pairs if v is not None}

    cluster_members: dict[str, tuple[int, list[str]]] = {}
    for item in judged_items:
        found = store.cluster_of(item.id)
        if found and len(found[1]) >= 2:
            cluster_members[item.id] = (len(found[1]), found[1])

    def gates_for(source: str) -> dict[str, float]:
        fitted = store.calibration(backend.name, model, jcfgs.get_config(source).version)
        return fitted or treemap.DEFAULT_GATES

    meta = treemap.MapMeta(
        date_label=label,
        state_hash=latest[0] if latest else "",
        spend_usd=store.spend_between(start_ts, int(time.time())),
        outages=list(report.outages),
        stale_state=report.stale_state,
        backend_down=report.backend_down,
        revived=len([lab for lab in store.labels(since=start_ts) if lab["label"] == "revive"]),
        pending=len(items) - len(judged_items),  # fetched but unjudged: the empty map must say why
        window=(start_ts, end_ts),
    )
    nodes = treemap.build_nodes(judged_items, verdict_map, cluster_members, gates_for)
    data = treemap.build_map_data(meta, nodes)
    html = treemap.render_html(data, gates_for("arxiv") if nodes else treemap.DEFAULT_GATES)
    out = Path(cfg.output_dir) / f"sibilla-{label}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    report.map_path = str(out)
    report.clusters = store.cluster_counts(start_ts, end_ts)
    if cfg.delivery.channel == "telegram":
        result = deliver_telegram(cfg.delivery, out, treemap.delivery_summary(data))
        report.delivery = str(result)
    return out


def run_cycle(
    cfg: Config,
    date_label: str | None = None,
    deliver: bool = False,
    window_hours: int | None = None,
    source_overrides: dict[str, int] | None = None,
) -> RunReport:
    """The full daily cycle (docs/01): the five stages in order."""
    store = Store(cfg.db_path)
    try:
        _, end, label = window_for(cfg, date_label, window_hours)
        report = RunReport(date_label=label, window_hours=window_hours, source_overrides=dict(source_overrides or {}))

        backend = make_backend(cfg)
        backend_up = backend.health() if hasattr(backend, "health") else True

        report.fetched, report.outages = fetch_and_normalize(cfg, store, end, window_hours, report.source_overrides)

        state_hash, state_text, stale = ensure_state(cfg, store)
        report.state_hash = state_hash
        report.stale_state = stale

        backend_down = not backend_up
        if backend_up:
            try:
                report.judged, report.judge_errors = judge_pending(cfg, store, backend, state_hash, state_text)
            except JudgeUnavailable:
                backend_down = True
        report.backend_down = backend_down

        start_ts = widest_window_start(cfg, end, window_hours, report.source_overrides)
        report.clusters = build_clusters(cfg, store, backend if backend_up else None, start_ts, int(end.timestamp()))
        report.spend_usd = store.spend_between(start_ts, int(time.time()))

        if deliver and cfg.delivery.channel != "telegram":
            report.delivery = "delivery channel not configured — open the HTML manually"
        build_map(cfg, store, backend, report)
        return report
    finally:
        store.close()
