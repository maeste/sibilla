# 03 — Judge (pluggable)

**Project:** Sibilla
**Depends on:** [01 – Architecture](01-ARCHITECTURE.md), [02 – Sources](02-SOURCES.md)

## Why a System One judge (and not an LLM)

Sibilla's judge must return **typed answers with probabilities**: one label
per item, no prose, no explanations. This maps 1:1 onto the System One
model category (TypeSafe's Jev was the first; [CLM][clm] is the open,
self-hostable one):

| Sibilla needs | LLM | System One judge |
|---|---|---|
| One label per item, no prose | Parse generated text (fragile) | Typed primitives |
| Uncertainty handling | Parse "I think maybe..." | Probability distributions |
| 1,750 judgments/week | ~$59/yr even with mini model | Free (local CLM) or ~$1.43/yr (Jev) |
| Stable judgment scale | Drifts with wording | Trained for calibrated decisions |
| State privacy | Same | **Local: the state never leaves the machine** |

**The judge is a pluggable backend, not a vendor.** Sibilla talks to it
through a narrow protocol (below); the reference deployment uses a local
[CLM][clm] instance, and TypeSafe's hosted Jev is a supported alternative
(same wire format — a request written for one replays on the other).

[clm]: https://github.com/Contrastive-LM/CLM

## JudgeBackend protocol

Every backend implements the same three primitives, regardless of vendor:

| Primitive | Type | Question core | Feeds |
|---|---|---|---|
| `noul` | binary probability | "Does this introduce techniques absent from the state?" | treemap **color** |
| `score` | rubric score | "How relevant is this to the active work in the state? (0–10)" | treemap **size** |
| `choice` | closed taxonomy | source-specific `kind` routing | routing rules |

```python
class JudgeBackend(Protocol):
    def system_one(self, state: str, questions: dict[str, Question]) -> Answers: ...
    def rank(self, context: str, question: str, candidates: list[str]) -> Ranked: ...
    def health(self) -> bool: ...
```

`judge/system_one()` is used for the per-item verdicts; `judge/rank()` is
the dedup engine's similarity primitive (see [Dedup via rank](#dedup-via-rank)).

Config lives in `[judge]` of the Sibilla config:

```toml
[judge]
backend = "clm"                # "clm" | "typesafe" (| future backends)
base_url = "http://127.0.0.1:8700"
api_key_env = "CLM_API_KEY"    # optional; unset for a local instance
model = "clm-latest"           # head selection (per-source overrides below)
```

## Backends

### CLM (local, reference deployment)

[CLM][clm] (Contrastive Language Model, Apache 2.0) serves **CLM-8B**
behind a **TypeSafe-compatible API**: `Noul` / `Choice` / `Score` wire
types match Jev's, so backend switching is a config change, not a port.

Deployment is two processes:

```bash
# 1. encoder (Qwen3-8B embeddings, GPU)
vllm serve Qwen/Qwen3-8B --served-model-name qwen3-8b \
  --runner pooling --max-model-len 8192 --port 8090

# 2. CLM API (75 MB reference head, downloaded on first run)
clm-serve --max-tokens 8192 --port 8700
```

- The heads run on GPU when torch sees one, else CPU (`--device cpu`);
  latency goes from ~30 ms (fresh state, RTX 4090) to a few hundred ms.
- `CLM_API_KEY` is optional; a loopback instance without it is fine.
- The **playground UI** at `http://127.0.0.1:8700/` is the fastest way to
  debug a JudgeConfig: paste a state, type the questions, look at the
  distributions before committing them to a source plugin.

#### GPU topology

The encoder is the only part that really wants a GPU; the projection
heads (20M params) are cheap anywhere. Pick the topology that matches
the hardware:

| Setup | Encoder (vLLM) | Heads (`clm-serve`) | Item latency |
|---|---|---|---|
| GPU on this machine | local | local, GPU | ~30 ms |
| GPU elsewhere | remote host | local, CPU | ~30 ms + RTT |
| No GPU at all | local, CPU | local, CPU | hundreds of ms |
| Hosted Jev instead | — | — (no CLM) | ~350–440 ms |

The split topology (row 2) is the interesting one for a GPU-less server
like the one Sibilla targets: `clm-serve` reaches the remote encoder
with `--emb-url http://<gpu-host>:8090/v1/embeddings`. Over Tailscale
the embedding payloads (a few hundred KB) add a single-digit-ms RTT to
state misses; cache hits never touch the network. Wire the GPU host into
the same tailnet and the whole judge stays private. `--device cpu` on
`clm-serve` is the explicit fallback for all-CPU setups, not the default
plan: on CPU-only the weekly 1,750-item cycle goes from minutes to hours.

What Sibilla uses beyond the basic three primitives:

- **Vector cache (built-in).** The state embedding is computed once and
  cached while the state is stable — exactly Sibilla's access pattern
  (one state, thousands of items). Item requests then skip the encoder
  for the state side and answer in ~1 ms server-side. The monthly state
  rebuild invalidates the cache naturally.
- **Per-source heads via `--ckpt-dir` + `model`.** `clm-serve` hot-reloads
  every `*.pt` in a directory and each request picks its head by name.
  The arXiv source can run a head fine-tuned on your READ history while
  HN still runs the reference head (see [Fine-tuning](#fine-tuning-the-sibilla-tune-loop)).
- **`POST /v1/rank`** — score free-form candidates against a state in one
  call. Used by dedup ([below](#dedup-via-rank)) and available to future
  source plugins for "which of these N variants is canonical".
- **`temperature`** per request — sharpens or flattens distributions;
  useful during calibration experiments, default 1 in production.

### TypeSafe Jev (hosted alternative)

Same primitives, hosted, pay-per-token ($0.042/Mtok input, output free;
~$1.43/yr at Sibilla's volume). Choose it when no GPU is available or
when you want zero local infrastructure: `backend = "typesafe"` and the
SDK key in `api_key_env`. Everything else in this document — state
construction, packing, cache keys, confidence gating — is
backend-agnostic except where noted.

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
everything daily and destroy the cache (CLM) or the cost model (Jev).

**Backend note:** the 4k-token state exceeds CLM's default 2048-token
window — the reference deployment raises both vLLM (`--max-model-len
8192`) and `clm-serve` (`--max-tokens 8192`). CLM renders objects as
prose (`key: value` lines), which matches the builder's output format
directly; the heads are trained on prose, never JSON.

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

## Fan-out scheduling

Jev packs many items per 64k request; CLM inverts the shape — one state
embed, one request per item, encoder time amortized by the vector cache.
The scheduler abstracts both behind the same interface:

```
per weekly cycle (1,750 arXiv items):
  Jev (typesafe):    ~10 packed requests of ~170 items   → ~650k input tok ≈ $0.027
  CLM (local):       state embed once (cache miss)        → then ~1,750 item requests
                     @ ~30–60 ms each ≈ 1–2 min GPU wall time, $0
```

**Scheduling rules (both backends):**
- Items are grouped per source (a request never mixes JudgeConfigs —
  different question sets).
- Per-source char budgets from JudgeConfig enforce truncation
  deterministically before the item ever reaches the backend.
- Every response is persisted raw (request hash, latency, token counts
  or `usage`) → the store is also the cost ledger, whichever backend.
- Failure isolation: one poisoned item can't take down the cycle; errors
  are recorded per item and the map renders with what succeeded.

## Cost model

Assumptions: 3 arXiv categories ≈ 250 papers/day = 1,750/week; state
4,000 tokens refreshed monthly.

**CLM (local, reference):** marginal cost $0. The real costs are the GPU
(see [GPU topology](#gpu-topology): a consumer card on the Sibilla host,
or any GPU host on the tailnet; all-CPU works but turns the weekly cycle
into hours, not minutes) and ~2 GB VRAM for the reserved vector cache
(`--action-cache` default 2%). At Sibilla's volume the machine is idle
99.9% of the time; the weekly judge cycle is a 1–2 minute job with a GPU
in the loop.

**TypeSafe Jev (alternative):**

```
input_tokens/week ≈ (1,750 × 350) + (10 req × 4,000 state) ≈ 650k
cost/week          ≈ 650k / 1M × $0.042 ≈ $0.027
cost/year          ≈ $1.43
```

Adding HN + Reddit (v1) roughly doubles volume; X (v2, monitored profiles
only) is bounded by the profile list. **The binding constraint is never
judge cost; it is fetch APIs (X especially).**

## Confidence gating

Every primitive returns a confidence. Sibilla uses it as a first-class UI
dimension, not a log line:

- `confidence ≥ 0.8` → normal quadrant placement
- `0.5 ≤ confidence < 0.8` → item placed **with a dashed border**
  ("model is unsure")
- `confidence < 0.5` → item moves to the **human zone** (gray band,
  excluded from KILL statistics — low confidence must never silently kill
  anything)

**Backend note — calibrate before trusting the thresholds.** CLM's
`confidence` is `top_prob − mean(others)` (a margin, not a calibrated
probability; its own model card reports ECE ≈ 0.21 for the base
checkpoint vs Jev's ≈ 0.14). The 0.8/0.5 defaults are therefore starting
points: `sibilla tune` fits them against observed precision on your own
READ history, per source and per backend. Thresholds live in the store
keyed by `(backend, model, judgeconfig_version)` so a head swap or
backend switch re-opens calibration instead of silently reusing numbers
that meant something else.

## Fine-tuning: the `sibilla tune` loop

CLM heads fine-tune cheaply on your own data (`train/finetune.py`,
frozen encoder, 20M-param head; see CLM's docs/FINETUNING.md). Sibilla
closes the loop:

1. Every READ/SKIM/KILL click you make in the map is a labelled example
   already in the store (item, state_hash, your label).
2. `sibilla tune --source arxiv` exports the labelled pairs as a typed
   decisions dataset, runs the CLM fine-tune, and drops the head into
   `clm-serve`'s `--ckpt-dir`.
3. The hot-reloaded head serves the next cycle under a new `model` name;
   verdicts key on it, so old and new judgments coexist and a diff shows
   whether fine-tuning actually moved your quadrants.

The loop is what turns "generic System One model" into "a judge trained
on what *you* actually read" — and it stays optional: the reference head
is already usable zero-shot.

## Dedup via rank

The dedup engine's residual clustering (cross-source echoes: same story
on arXiv and HN) uses `judge.rank()`:

```
context   = canonical item title + first sentences
question  = "Is this the same underlying story?"
answers   = candidate items from other sources
→ prob per candidate; prob ≥ threshold ⇒ same cluster
```

One rank call scores a whole candidate set against the canonical item
(candidate embeddings are cached), replacing the pairwise-comparison
naïve approach. Thresholds tune the same way as confidence gating.

## Cache & re-judge semantics

`verdicts` unique key: `(item_id, state_hash, judgeconfig_version,
backend, model)`.

- Same state, same config, same backend → cache hit, zero cost.
- New month, new state → full re-judge (irrelevant either way: free
  locally, ~$0.027 on Jev).
- JudgeConfig version bump (e.g. reworded question) → re-judge only the
  source whose config changed.
- Backend or model switch → re-judge under the new key; the old verdicts
  stay queryable for A/B.

This also gives free experimentation: judge the same week under two
states (e.g. with/without RADAR.md) or two heads, and diff the quadrants
— measurable "how much does my profile move my filter".
