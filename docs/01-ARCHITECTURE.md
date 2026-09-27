# 01 — Architecture

**Project:** Sibilla
**Depends on:** [00 – Vision](00-VISION.md)

## System overview

```
                      ┌─────────────────────────────────────────┐
                      │                PIPELINE                 │
                      │                                         │
 ┌──────────┐  fetch  │  ┌────────┐   ┌───────────┐   ┌──────┐ │  render  ┌─────────┐
 │  SOURCES │ ──────▶ │  │ NORMAL │──▶│  JUDGE    │──▶│ MAP  │ │ ───────▶ │ treemap │
 │ (plugins)│         │  │ IZE    │   │  JUDGE    │   │ BUIL │ │          │ .html   │
 └──────────┘         │  └────────┘   └───────────┘   └──┬───┘ │          └─────────┘
   arxiv / hn /       │      │              ▲            │     │               ▲
   reddit / x         │      │        ┌─────┴────┐       │     │               │
                      │      │        │   STATE  │       │     │        ┌──────┴─────┐
                      │      │        │ builder  │       │     │        │  DELIVERY  │
                      │      │        └─────┬────┘       │     │        │ cron → TG  │
                      │      │              │            ▼     │        └────────────┘
                      │      │         ┌────┴────┐  ┌────────┐ │
                      │      └────────▶│  STORE  │  │  DEDUP │ │
                      │                │ sqlite  │◀─│ engine │ │
                      │                └─────────┘  └────────┘ │
                      └─────────────────────────────────────────┘
```

Five stages, each independently testable:

1. **Fetch** — source plugins pull raw items into the store (idempotent,
   keyed by source-native ID).
2. **Normalize** — heterogeneous items → common `Item` shape (id, title,
   body-ref, url, author, ts, source, metadata).
3. **State** — deterministic builder produces the judgment state from
   GitHub + `RADAR.md` (see [03](03-JUDGE.md#state-construction)).
4. **Judge** — per-source JudgeConfig runs the pluggable judge backend
   (reference: local CLM) over the items; typed
   verdicts land in the store (see [03](03-JUDGE.md)).
5. **Map build** — dedup engine clusters cross-source echoes; scoring
   formula + thresholds assign quadrant + treemap geometry (see
   [04](04-VISUAL.md)).

## Core design rules

1. **Deterministic ground truth, semantic decoration.** IDs, timestamps,
   token accounting, dedup keys: computed in code. Only labels/scores come
   from the model. The model never invents structure.
2. **Sources are plugins.** A source is a Python package exposing `fetch()
   -> list[RawItem]` + its own `JudgeConfig`. Adding X must not touch the
   core pipeline.
3. **JudgeConfig is per-source.** Content nature differs (paper abstract vs
   280-char hot take); the question set, scales and thresholds differ with
   it. State stays shared and unique.
4. **Everything lands in SQLite first.** Re-runs, cost accounting, dedup
   history and drift analysis all query one local DB. No item is ever
   judged twice (cache by `hash(state_version, item_id, judgeconfig_version)`).
5. **Single-file HTML output.** The deliverable is one self-contained
   `sibilla-YYYY-MM-DD.html` (inlined JS/CSS, no server).

## Repository layout (implemented)

```
sibilla/
├── README.md
├── docs/                    # this design documentation
├── pyproject.toml
├── sibilla/
│   ├── cli.py               # `sibilla run`, `sibilla map`, `sibilla state`, `sibilla tune`, `sibilla revive`
│   ├── config.py            # sibilla.yaml loading (sources / state / judge / delivery)
│   ├── pipeline.py          # orchestration of the 5 stages
│   ├── store.py             # sqlite: items, verdicts, state versions, ledger, labels, calibrations
│   ├── delivery.py          # telegram delivery (manual/open-the-file otherwise)
│   ├── state/
│   │   ├── builder.py       # gh api → profile state (deterministic)
│   │   └── radar_md.py      # RADAR.md override parsing
│   ├── judge/
│   │   ├── base.py          # JudgeBackend protocol + question/answer types + wire client
│   │   ├── clm.py           # CLM backend (local, TypeSafe-compatible API)
│   │   ├── typesafe.py      # TypeSafe Jev backend (hosted alternative)
│   │   ├── packer.py        # per-backend scheduling (batch vs fan-out)
│   │   └── configs.py       # JudgeConfig registry (one per source) + scoring/routing
│   ├── sources/
│   │   ├── base.py          # SourcePlugin protocol + JudgeConfig dataclass
│   │   ├── arxiv.py         # v0
│   │   ├── hackernews.py    # v1
│   │   ├── reddit.py        # v1
│   │   └── twitter_x.py     # v2 (monitored-profiles list; pluggable transport)
│   ├── dedup/
│   │   └── cluster.py       # cross-source echo detection
│   └── render/
│       ├── treemap.py       # data → node JSON + single-file HTML assembly
│       ├── template.html.j2 # the map UI (layout at view time)
│       └── vendor/d3-hierarchy.min.js  # vendored, inlined at render time — no CDN
├── tests/
└── RADAR.md                 # optional manual interest override (user-edit)
```

Deviations from the original sketch are recorded in the doc where they occur
(d3 is vendored and layout runs at view time; config is one `sibilla.yaml`).

## Data model (SQLite)

```sql
items(id TEXT PK, source TEXT, native_id TEXT, fetched_at INT,
      title TEXT, url TEXT, author TEXT, published INT,
      body_ref TEXT,          -- path/rowid to full text, kept local
      meta JSON)

verdicts(id INTEGER PK, item_id TEXT, state_hash TEXT,
         judgeconfig_version TEXT,
         scores JSON,          -- {"relevance": 7.2, "novelty": 0.81, ...}
         confidences JSON,     -- per-primitive confidence from the judge
         created_at INT,
         UNIQUE(item_id, state_hash, judgeconfig_version,
                backend, model))   -- backend+model in the key: A/B across judges

state_versions(hash TEXT PK, payload TEXT, built_at INT)

clusters(id INTEGER PK, created_at INT)          -- dedup groups
cluster_items(cluster_id INT, item_id TEXT)
```

`state_hash` in the verdict key = free A/B: rebuild the state, re-judge,
compare quadrants → measure how much profile changes move the needle.

## Failure modes & handling

| Failure | Handling |
|---------|----------|
| Judge backend down | Backend-specific backoff; pipeline degrades to last verdicts; map renders with `stale` badge |
| Source API down | Per-source isolation; map renders with remaining sources + missing-source note |
| State builder fails (gh auth) | Fall back to last good state; loud warning in map header |
| Poisoned item (huge body) | Packer truncates to per-source char budget; flagged `truncated` |

## Security & privacy

- Item bodies and state stay **local** (SQLite). With the reference CLM
  backend nothing transits anywhere: source APIs in, everything else
  stays on the machine. The hosted TypeSafe backend receives
  titles/abstracts + state text — opt into it consciously.
- X source credentials live in env vars, never in the store.
- Session-like secrets never enter the map HTML (it is meant to be
  shareable/exportable).
