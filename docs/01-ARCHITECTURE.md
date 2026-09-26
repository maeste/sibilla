# 01 — Architecture

**Project:** Sibilla
**Depends on:** [00 – Vision](00-VISION.md)

## System overview

```
                      ┌─────────────────────────────────────────┐
                      │                PIPELINE                 │
                      │                                         │
 ┌──────────┐  fetch  │  ┌────────┐   ┌───────────┐   ┌──────┐ │  render  ┌─────────┐
 │  SOURCES │ ──────▶ │  │ NORMAL │──▶│  JEV      │──▶│ MAP  │ │ ───────▶ │ treemap │
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
   GitHub + `RADAR.md` (see [03](03-JEV-JUDGE.md#state-construction)).
4. **Judge** — per-source JudgeConfig runs Jev over packed batches; typed
   verdicts land in the store (see [03](03-JEV-JUDGE.md)).
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

## Repository layout (planned)

```
sibilla/
├── README.md
├── docs/                    # this design documentation
├── pyproject.toml
├── sibilla/
│   ├── cli.py               # `sibilla run`, `sibilla map`, `sibilla state`
│   ├── pipeline.py          # orchestration of the 5 stages
│   ├── store.py             # sqlite: items, verdicts, state versions
│   ├── state/
│   │   ├── builder.py       # gh api → profile state (deterministic)
│   │   └── radar_md.py      # RADAR.md override parsing
│   ├── judge/
│   │   ├── client.py        # TypeSafe SDK wrapper, retries, rate-limit aware
│   │   ├── packer.py        # fan-out packing (state + N items / request)
│   │   └── configs.py       # JudgeConfig registry (one per source)
│   ├── sources/
│   │   ├── base.py          # SourcePlugin protocol + JudgeConfig dataclass
│   │   ├── arxiv.py         # v0
│   │   ├── hackernews.py    # v1
│   │   ├── reddit.py        # v1
│   │   └── twitter_x.py     # v2 (monitored-profiles list)
│   ├── dedup/
│   │   └── cluster.py       # cross-source echo detection
│   └── render/
│       ├── treemap.py       # data → d3-hierarchy JSON
│       └── template.html.j2 # single-file output
├── tests/
└── RADAR.md                 # optional manual interest override (user-edit)
```

## Data model (SQLite)

```sql
items(id TEXT PK, source TEXT, native_id TEXT, fetched_at INT,
      title TEXT, url TEXT, author TEXT, published INT,
      body_ref TEXT,          -- path/rowid to full text, kept local
      meta JSON)

verdicts(id INTEGER PK, item_id TEXT, state_hash TEXT,
         judgeconfig_version TEXT,
         scores JSON,          -- {"relevance": 7.2, "novelty": 0.81, ...}
         confidences JSON,     -- per-primitive confidence from Jev
         created_at INT,
         UNIQUE(item_id, state_hash, judgeconfig_version))

state_versions(hash TEXT PK, payload TEXT, built_at INT)

clusters(id INTEGER PK, created_at INT)          -- dedup groups
cluster_items(cluster_id INT, item_id TEXT)
```

`state_hash` in the verdict key = free A/B: rebuild the state, re-judge,
compare quadrants → measure how much profile changes move the needle.

## Failure modes & handling

| Failure | Handling |
|---------|----------|
| Jev 429 / outage | SDK backoff; pipeline degrades to last verdicts; map renders with `stale` badge |
| Source API down | Per-source isolation; map renders with remaining sources + missing-source note |
| State builder fails (gh auth) | Fall back to last good state; loud warning in map header |
| Poisoned item (huge body) | Packer truncates to per-source char budget; flagged `truncated` |

## Security & privacy

- Item bodies and state stay **local** (SQLite); only titles/abstracts +
  state text transit to the Jev API. No third-party services beyond the
  source APIs and TypeSafe.
- X source credentials live in env vars, never in the store.
- Session-like secrets never enter the map HTML (it is meant to be
  shareable/exportable).
