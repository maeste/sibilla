# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

Sibilla is a personal research radar: it filters the daily AI/tech content flood (arXiv, HN, Reddit, X) against a judgment state built deterministically from the user's GitHub activity, using a System One judge backend (local CLM as reference deployment, hosted TypeSafe Jev as alternative), and renders the result as a single-file HTML treemap.

**Implemented** (v0 → v2 code complete, see docs/05): the `sibilla/` package is the pipeline (fetch → normalize → state → judge → map build), `docs/` remains the source of truth — implementation changes that touch design decisions go through the docs first. Note the parent-directory `../CLAUDE.md` targets the sibling smolagents project; its tooling commands (make quality, etc.) do not apply here.

## Documentation map

The docs form a dependency chain — read in order:

- `00-VISION.md` — problem, thesis, **non-goals**. The non-goals are a contract (risk R6): every feature request must map to "filter better" or be rejected.
- `01-ARCHITECTURE.md` — five-stage pipeline, design rules, planned repo layout, SQLite schema, failure handling.
- `02-SOURCES.md` — SourcePlugin contract, per-source JudgeConfig rationale, source specs (arXiv v0 → X v2).
- `03-JUDGE.md` — JudgeBackend protocol, CLM/Jev backends, state construction, primitives, scheduling, cost model, confidence gating, fine-tune loop.
- `04-VISUAL.md` — treemap spec, quadrant encoding, KILL discipline, human zone, delivery.
- `05-ROADMAP.md` — phases (v0 → v2), risks, validation criteria.

## Non-negotiable design rules

Decided in design; do not relitigate in code without updating the docs first:

1. **Deterministic ground truth, semantic decoration.** IDs, timestamps, cache keys, dedup keys, truncation are computed in code; only scores/labels come from the judge. The model never invents structure.
2. **State is built, never generated.** Interest profile = GitHub (topics, README leads, commit subjects) + optional `RADAR.md`, appended verbatim. `state_hash = sha256(state_text)` keys every verdict; monthly rebuild cadence (daily churn would destroy the cache/cost model).
3. **No summarization.** The judge returns typed answers with probabilities (`score` / `noul` / `choice` primitives), never prose. Digest prose is explicitly deferred.
4. **Sources are plugins; JudgeConfig is per-source.** Questions, scales and thresholds differ per content species; the state stays shared and unique. Adding a source must not touch the pipeline core.
5. **Everything lands in SQLite first.** One local DB is the store, cost ledger and dedup history. Verdict cache key: `(item_id, state_hash, judgeconfig_version, backend, model)` — no item is judged twice under the same key; backend/model changes give free A/B.
6. **Single-file HTML output.** `sibilla-YYYY-MM-DD.html`, inlined JS/CSS, no server, no network calls at view time, no secrets in the output (it is meant to be shareable).
7. **Confidence is a UI dimension.** 0.5–0.8 → dashed border; < 0.5 → bounded human zone excluded from KILL statistics. Low confidence must never silently kill an item.

## Planned structure

Implemented as designed: Python package `sibilla/` with `cli.py`, `pipeline.py`, `store.py`, `config.py`, `delivery.py`, and `state/`, `judge/`, `sources/`, `dedup/`, `render/` subpackages; tests in `tests/`. CLI: `sibilla run`, `sibilla map`, `sibilla state`, `sibilla tune`, `sibilla revive`, `sibilla prune`.

## Commands

```bash
pip install -e ".[dev]"      # deps: requests, PyYAML, Jinja2 (dev: pytest, ruff)
make quality                 # NO — that's the sibling smolagents project; here:
ruff check sibilla/ tests/   # lint (line length 119, py310 target)
ruff format sibilla/ tests/  # format
python3 -m pytest tests/     # full suite (~75 tests, no network — fetchers run on recorded payloads)
python3 -m pytest tests/test_judge.py -q                    # one file
python3 -m pytest tests/test_judge.py::test_hn_drama_never_reads  # one test
python3 -m sibilla.cli --help                # the CLI (no console script in sandboxed envs)
python3 -m sibilla.cli run --date 2026-09-27 # full cycle
```

Tests never touch the network: a deterministic `FakeBackend` (tests/conftest.py)
stands in for the judge, and HTTP sources run against recorded payloads. The
real arXiv/CLM surfaces were validated once in a live smoke.

## Judge backend context

- Reference deployment: two processes — vLLM serving `Qwen/Qwen3-8B` embeddings on :8090, `clm-serve` on :8700 (TypeSafe-compatible wire API; a request written for one backend replays on the other). The playground UI at `http://127.0.0.1:8700/` is the fastest way to debug a JudgeConfig before committing it to a source plugin.
- The 4k-token state exceeds CLM's default 2048-token window: raise both vLLM (`--max-model-len 8192`) and `clm-serve` (`--max-tokens 8192`).
- CLM's `confidence` is a margin (top_prob − mean(others)), not a calibrated probability — the 0.8/0.5 gating thresholds are starting points to re-fit per `(backend, model, judgeconfig_version)`.

## Conventions

- Commits use lowercase type prefixes (`docs:`, `design:`); work lands via `design/*` branches merged by PR.
- Design changes are doc changes first: update the relevant `docs/0N-*.md`, then implement. Docs cross-reference each other with relative links — keep the chain intact when renaming.
