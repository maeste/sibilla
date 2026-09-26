# 03 — Jev Judge

**Project:** Sibilla
**Depends on:** [01 – Architecture](01-ARCHITECTURE.md), [02 – Sources](02-SOURCES.md)

## Why Jev (and not an LLM)

[Jev](https://docs.typesafe.ai/) is a **System One model**: it evaluates a
state and returns typed answers with calibrated probabilities. It does not
generate text, code or explanations. This maps 1:1 onto Sibilla's needs:

| Sibilla needs | LLM | Jev |
|---|---|---|
| One label per item, no prose | Parse generated text (fragile) | Typed primitives |
| Uncertainty handling | Parse "I think maybe..." | Calibrated confidence field |
| 1,750 judgments/week | ~$59/yr even with mini model | **~$1.43/yr** |
| Stable judgment scale | Drifts with wording | Trained for calibrated decisions |

The economics are structural, not incidental: Jev charges **input tokens
only, output is free**, and it **packs a state once + many questions per
request**. An LLM must re-read the profile for every item; Jev amortizes
it. That 42x gap is what makes "judge everything, show proportions"
viable instead of "sample and hope".

## State construction

The state is the user's interest profile — **built deterministically,
never generated**:

```
build_state(github_users, github_orgs, radar_md_path) -> State
```

1. `gh api` / GitHub REST: for each repo (pushed within 6 months,
   excluding forks/archived):
   - repo name + description + topics
   - README first section (up to ~800 chars, headings-stripped)
   - last 20 commit subjects
2. Compact to a structured text (~4,000 tokens target):

   ```
   ACTIVE WORK PROFILE (auto-generated from GitHub, 2026-09-26)

   ## maeste/agent-me-kb — personal knowledge base for agents
   topics: knowledge-graph, agents, arxiv
   recent: fix wiki ingestion; add compass scoring; ...

   ## RisorseArtificiali/agent-ready-skill — ...
   ```

3. `RADAR.md` (optional, user-edited) appended verbatim — the escape hatch
   for interests invisible from code (e.g. "evaluating vector DBs for a
   Q4 project", "deep-diving context engineering").

4. `sha256(state_text)` → `state_hash`. Every verdict is keyed to it; a
   rebuilt state re-judges only what changed underneath.

**Refresh cadence:** monthly, or on-demand via `sibilla state --rebuild`.
The state is stable by design — daily profile churn would re-judge
everything daily and destroy the cost model.

## Primitives (per-source, recap from 02)

Each source's JudgeConfig defines its questions. Common shape:

| Primitive | Type | Question core | Feeds |
|---|---|---|---|
| relevance | score 0–10 | "How relevant is this to the active work in the state?" | treemap **size** |
| applicability | score 0–10 | "Could this be applied in the state's projects within ~3 months?" (arXiv only) | READ boost |
| novelty | noul | "Does this introduce techniques/concepts absent from the state?" | treemap **color** |
| substance | noul | "Concrete verifiable signal vs. opinion churn?" (HN/X) | KILL override |
| kind | choice | source-specific taxonomy | routing rules |

Composite score per source: `size_score = relevance` (primary);
READ/SKIM/KILL per the source's thresholds, with hard overrides
(`kind: drama` → KILL regardless of score).

## Fan-out packing

Jev ingests the state once and evaluates every question against it in
parallel; the 64k request budget covers state + all items. The packer
makes this concrete:

```
tokens:
  state              ~4,000   (fixed per cycle)
  item + questions   ~350     (arXiv abstract; less for HN/X items)
  ─────────────────────────
  per request        ≤64,000
  → ~170 arXiv items/request; ~10 requests/week for 1,750 items
```

**Packing rules:**
- Items are grouped per source (a request never mixes JudgeConfigs —
  different question sets).
- Requests stay under budget with 10% headroom; char budgets from
  JudgeConfig enforce truncation deterministically before tokenization.
- Every response is persisted raw (request hash, latency, token counts)
  → the store is also the cost ledger.

## Cost model

Assumptions: 3 arXiv categories ≈ 250 papers/day = 1,750/week; state
4,000 tokens refreshed monthly; Jev at **$0.042/Mtok input, output free**
(typesafe.ai pricing page, Sept 2026):

```
input_tokens/week ≈ (1,750 × 350) + (10 req × 4,000 state) ≈ 650k
cost/week          ≈ 650k / 1M × $0.042 ≈ $0.027
cost/year          ≈ $1.43
```

Adding HN + Reddit (v1) roughly doubles volume; X (v2, monitored profiles
only) is bounded by the profile list. Even a 10x volume explosion lands
under $30/year. **The binding constraint is never Jev cost; it is fetch
APIs (X especially).**

## Confidence gating

Every primitive returns calibrated confidence. Sibilla uses it as a
first-class UI dimension, not a log line:

- `confidence ≥ 0.8` → normal quadrant placement
- `0.5 ≤ confidence < 0.8` → item placed **with a dashed border**
  ("model is unsure")
- `confidence < 0.5` → item moves to the **human zone** (gray band,
  excluded from KILL statistics — low confidence must never silently kill
  anything)

Calibration is measured across groups of predictions (per TypeSafe
docs), so thresholds are tunable against observed precision on your own
READ history — a `sibilla tune` loop for later phases.

## Cache & re-judge semantics

`verdicts` unique key: `(item_id, state_hash, judgeconfig_version)`.

- Same state, same config → cache hit, zero cost.
- New month, new state → full re-judge (~$0.027 — irrelevant).
- JudgeConfig version bump (e.g. reworded question) → re-judge only the
  source whose config changed.

This also gives free experimentation: judge the same week under two
states (e.g. with/without RADAR.md) and diff the quadrants — measurable
"how much does my profile move my filter".
