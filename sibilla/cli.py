"""The sibilla CLI (docs/01): run, map, state, tune, revive."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sibilla import __version__
from sibilla.config import Config, load_config
from sibilla.judge import configs as jcfgs
from sibilla.pipeline import build_map, ensure_state, make_backend, run_cycle, window_for
from sibilla.render import treemap
from sibilla.store import Store, VerdictKey, VerdictRow

LABEL_VALUES = ("read", "skim", "kill", "revive")


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 1
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="sibilla", description="Personal research radar (see docs/)")
    p.add_argument("--version", action="version", version=f"sibilla {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--config", default=None, help="path to sibilla.yaml (default: ./sibilla.yaml)")
    p.add_argument("--config", default=None, help=argparse.SUPPRESS)  # also accepted before the verb
    sub = p.add_subparsers(dest="command")

    run = sub.add_parser("run", parents=[common], help="fetch → normalize → state → judge → map build")
    run.add_argument("--date", default=None, help="map date YYYY-MM-DD (default: now)")
    run.add_argument("--deliver", action="store_true", help="deliver via the configured channel")
    run.add_argument("--rebuild-state", action="store_true", help="force a state rebuild")
    run.add_argument("--window-hours", type=int, default=None, help="override the fetch/map window (168 = weekly)")
    run.set_defaults(func=_cmd_run)

    mp = sub.add_parser("map", parents=[common], help="rebuild the map for a date — a pure function of the DB")
    mp.add_argument("--date", default=None, help="map date YYYY-MM-DD (default: now)")
    mp.add_argument("--out", default=None, help="override output path for the HTML")
    mp.add_argument("--window-hours", type=int, default=None, help="override the map window (168 = weekly recap)")
    mp.set_defaults(func=_cmd_map)

    st = sub.add_parser("state", parents=[common], help="show or rebuild the interest-profile state")
    st.add_argument("--rebuild", action="store_true", help="force a rebuild (gh api → text)")
    st.add_argument("--show", action="store_true", help="print the state text")
    st.add_argument("--history", action="store_true", help="drift view: state versions vs quadrant shifts")
    st.set_defaults(func=_cmd_state)

    tn = sub.add_parser("tune", parents=[common], help="confidence-gate fitting + CLM fine-tune dataset export")
    tn.add_argument("--source", default=None, help="restrict to one source")
    tn.add_argument("--import-labels", dest="import_labels", default=None, help="labels JSON exported from the map")
    tn.add_argument("--export", default=None, help="write a typed-decisions dataset for CLM train/finetune.py")
    tn.add_argument("--fit", action="store_true", help="fit confidence gates against your labels")
    tn.add_argument(
        "--train", action="store_true", help="export + run judge.finetune_cmd and drop the head in ckpt_dir"
    )
    tn.add_argument("--head-out", dest="head_out", default=None, help="where the trained head lands (default: tmp)")
    tn.add_argument(
        "--head-name", dest="head_name", default=None, help="head filename in ckpt_dir (default: head-out stem)"
    )
    tn.set_defaults(func=_cmd_tune)

    rv = sub.add_parser("revive", parents=[common], help="re-surface a killed item (false-negative telemetry)")
    rv.add_argument("item_id", help="item id, e.g. arxiv:2401.12345v1")
    rv.add_argument("--reason", default=None, help="why (kept in the labels log)")
    rv.set_defaults(func=_cmd_revive)
    return p


def _cfg(args: argparse.Namespace) -> Config:
    return load_config(args.config)


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    report = run_cycle(cfg, date_label=args.date, deliver=args.deliver, window_hours=args.window_hours)
    print(report.summary())
    return 0


def _cmd_map(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    try:
        _, _, label = window_for(cfg, args.date, args.window_hours)
        backend = make_backend(cfg)
        from sibilla.pipeline import RunReport

        report = RunReport(date_label=label, window_hours=args.window_hours)
        path = build_map(cfg, store, backend, report)
        if args.out and path is not None:
            target = Path(args.out)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(Path(path).read_text(encoding="utf-8"), encoding="utf-8")
            path = target
        print(f"map → {path}")
        if report.delivery:
            print(f"delivery: {report.delivery}")
        return 0
    finally:
        store.close()


def _cmd_state(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    try:
        if args.history:
            drift_view(store)
            return 0
        state_hash, payload, stale = ensure_state(cfg, store, rebuild=args.rebuild)
        print(f"state {state_hash[:12]} ({'stale fallback' if stale else 'current'})")
        if args.show:
            print()
            print(payload)
        return 0
    finally:
        store.close()


def drift_view(store: Store, max_states: int = 12) -> None:
    """Weekly drift view (docs/05 v2): state_hash history vs quadrant shifts.

    For every state version that has verdicts, reclassify them and print the
    quadrant mix — how much your profile moved the filter, per (backend, model).
    """
    rows = store.conn.execute(
        "SELECT state_hash, backend, model, COUNT(*) AS n FROM verdicts GROUP BY state_hash, backend, model"
        " ORDER BY MIN(created_at)"
    ).fetchall()
    if not rows:
        print("no verdicts yet — nothing to compare")
        return
    print(f"{'state':<14} {'backend/model':<28} {'judged':>6}  {'READ':>5} {'SKIM':>5} {'KILL':>5} {'HUMAN':>5}")
    for row in rows:
        state_hash, backend, model, n = row["state_hash"], row["backend"], row["model"], row["n"]
        counts = {"READ": 0, "SKIM": 0, "KILL": 0, "HUMAN": 0}
        items = store.conn.execute(
            "SELECT v.item_id, v.scores, i.source FROM verdicts v JOIN items i ON i.id = v.item_id"
            " WHERE v.state_hash = ? AND v.backend = ? AND v.model = ?",
            (state_hash, backend, model),
        ).fetchall()
        for r in items:
            v = VerdictRow(
                item_id=r["item_id"],
                key=VerdictKey(state_hash, "*", backend, model),
                scores=json.loads(r["scores"] or "{}"),
                confidences={},
            )
            answers = treemap.answers_from_verdict(r["source"], v)
            if answers:
                quadrant, _ = jcfgs.classify(r["source"], answers)
                counts[quadrant] += 1
        print(
            f"{state_hash[:12]:<14} {backend + '/' + model:<28} {n:>6}  "
            f"{counts['READ']:>5} {counts['SKIM']:>5} {counts['KILL']:>5} {counts['HUMAN']:>5}"
        )
    states = store.state_history()[-max_states:]
    if len(states) > 1:
        print(f"\n({len(states)} state versions on record — a rebuilt state re-judges under a new key)")


def _cmd_tune(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    try:
        if args.import_labels:
            n = import_labels(store, Path(args.import_labels))
            print(f"imported {n} new labels")
        if args.export or args.train:
            dataset = Path(args.export) if args.export else Path("sibilla-decisions.json")
            n = export_finetune_dataset(store, dataset, source=args.source)
            print(f"exported {n} typed decisions → {dataset}")
            if args.train:
                return run_clm_finetune(cfg, dataset, args.head_out, args.head_name)
        if args.fit or not (args.import_labels or args.export or args.train):
            results = fit_gates(cfg, store, source=args.source)
            for (src, backend, model, jcv), gates in results.items():
                print(f"{src} [{backend}/{model} @{jcv}]: gates {gates}")
            if not results:
                print("no labelled examples yet — click labels in the map, export, `tune --import`, then --fit")
        return 0
    finally:
        store.close()


def run_clm_finetune(cfg: Config, dataset: Path, head_out: str | None, head_name: str | None) -> int:
    """Run the CLM fine-tune on the exported dataset and drop the head in ckpt_dir (docs/03).

    ``judge.finetune_cmd`` is a command template with ``{dataset}`` and
    ``{head_out}`` placeholders (e.g. ``python {clm}/train/finetune.py
    --data {dataset} --out {head_out}``). Without it, the dataset is left for
    a manual run of CLM's train/finetune.py — the loop stays optional.
    """
    import shutil
    import subprocess
    import tempfile

    cmd_template = cfg.judge.finetune_cmd
    if not cmd_template:
        print(
            "judge.finetune_cmd not configured — dataset ready for a manual CLM train/finetune.py run.\n"
            '  set e.g.: finetune_cmd: "python /path/to/CLM/train/finetune.py --data {dataset} --out {head_out}"'
        )
        return 0
    head_out_path = Path(head_out) if head_out else Path(tempfile.mkdtemp(prefix="sibilla-head-")) / "head.pt"
    head_out_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = cmd_template.format(dataset=str(dataset), head_out=str(head_out_path))
    print(f"running: {rendered}")
    result = subprocess.run(rendered, shell=True, check=False)  # noqa: S602 — operator-authored command
    if result.returncode != 0:
        print(f"fine-tune failed with exit {result.returncode} — head not dropped", file=sys.stderr)
        return 1
    if not head_out_path.exists():
        print(f"fine-tune exited 0 but produced no head at {head_out_path}", file=sys.stderr)
        return 1
    ckpt_dir = cfg.judge.ckpt_dir
    if ckpt_dir:
        target = Path(ckpt_dir) / f"{head_name or head_out_path.stem}.pt"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(head_out_path, target)
        print(f"head dropped at {target} — clm-serve hot-reloads it")
        print(f"next cycle: set judge.model = {target.stem}  (verdicts key on it; old and new coexist for A/B)")
    else:
        print(f"head at {head_out_path} — set judge.ckpt_dir to have it dropped into clm-serve")
    return 0


def _cmd_revive(args: argparse.Namespace) -> int:
    cfg = _cfg(args)
    store = Store(cfg.db_path)
    try:
        item = store.item(args.item_id)
        if item is None:
            print(f"unknown item: {args.item_id}", file=sys.stderr)
            return 1
        state = store.latest_state()
        store.add_label(item.id, "revive", state_hash=state[0] if state else None)
        print(f"revived {item.id} — {item.title!r}")
        if args.reason:
            print(f"reason: {args.reason} (telemetry feeds threshold tuning)")
        return 0
    finally:
        store.close()


# --------------------------------------------------------------- tune loop


def import_labels(store: Store, path: Path) -> int:
    """Ingest the labels JSON exported from the map's `export labels` button."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    state = store.latest_state()
    n = 0
    for rec in payload.get("labels", []) if isinstance(payload, dict) else payload:
        label = str(rec.get("label", "")).lower()
        if label not in LABEL_VALUES:
            continue
        if store.add_label(str(rec["item_id"]), label, state_hash=state[0] if state else None):
            n += 1
    return n


def export_finetune_dataset(store: Store, path: Path, source: str | None = None) -> int:
    """Typed decisions dataset for CLM fine-tuning (docs/03): state + item + label."""
    decisions: list[dict[str, str]] = []
    for lab in store.labels(source=source):
        item = store.item(lab["item_id"])
        if item is None or not lab.get("state_hash"):
            continue
        decisions.append(
            {
                "state": lab["state_hash"],
                "item": (item.title or "") + "\n" + (item.body or ""),
                "source": item.source,
                "label": lab["label"],
            }
        )
    states = {r["hash"]: r["payload"] for r in store.conn.execute("SELECT hash, payload FROM state_versions")}
    path.write_text(
        json.dumps({"decisions": decisions, "states": states}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return len(decisions)


def fit_gates(
    cfg: Config, store: Store, source: str | None = None
) -> dict[tuple[str, str, str, str], dict[str, float]]:
    """Fit the 0.8/0.5 starting points against observed precision (docs/03).

    Grid-searches (low, high) maximizing agreement between the gated quadrant
    and your label on labelled items; ties prefer the larger high gate (the
    model must earn confidence). Stores one row per (backend, model, jc_version)
    so a head swap or backend switch re-opens calibration instead of silently
    reusing numbers that meant something else.
    """
    backend = make_backend(cfg)
    model = getattr(backend, "model", cfg.judge.model)
    results: dict[tuple[str, str, str, str], dict[str, float]] = {}
    sources = [source] if source else jcfgs.known_sources()
    for src in sources:
        jc = jcfgs.get_config(src)
        labels = [lab for lab in store.labels(source=src) if lab["label"] in ("read", "skim", "kill")]
        if len(labels) < 5:
            continue
        examples: list[tuple[float, str, str]] = []  # (confidence, predicted_quadrant, user_label)
        for lab in labels:
            item = store.item(lab["item_id"])
            if item is None:
                continue
            v = latest_verdict(store, item.id, backend.name, model)
            if v is None:
                continue
            answers = treemap.answers_from_verdict(src, v)
            quadrant, _ = jcfgs.classify(src, answers)
            examples.append((jcfgs.min_confidence(answers), quadrant, lab["label"]))
        if not examples:
            continue
        best = _grid_fit(examples)
        store.save_calibration(backend.name, model, jc.version, best, len(examples))
        results[(src, backend.name, model, jc.version)] = best
    return results


def _grid_fit(examples: list[tuple[float, str, str]]) -> dict[str, float]:
    """Deterministic grid search; accuracy first, then the higher `high` gate wins.

    HUMAN placements earn no credit — surfacing uncertainty is neutral, not
    good or bad (docs/03); the model must earn confidence either way.
    """
    best_score, best = (-1.0, -1.0), {"high": 0.8, "low": 0.5}
    for high in [round(0.6 + 0.05 * i, 2) for i in range(8)]:  # 0.60..0.95
        for low in [round(0.3 + 0.05 * i, 2) for i in range(7)]:  # 0.30..0.60
            if low >= high:
                continue
            correct = 0
            for conf, quadrant, label in examples:
                gated = "HUMAN" if conf < low else quadrant
                correct += gated.upper() == label.upper()
            score = (correct / len(examples), high)
            if score > best_score:
                best_score, best = score, {"high": high, "low": low}
    return best


def latest_verdict(store: Store, item_id: str, backend_name: str, model: str):
    from sibilla.pipeline import latest_verdict

    return latest_verdict(store, item_id, backend_name, model)


if __name__ == "__main__":
    raise SystemExit(main())
