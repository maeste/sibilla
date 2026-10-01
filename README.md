# Sibilla

> *The oracle that reads the stream so you don't have to.*

Sibilla is a personal research radar that filters the daily flood of AI/tech
content (arXiv, Hacker News, Reddit, X) through **what you actually build**,
using a **System One judge backend** (reference: [CLM][clm] running
locally; [TypeSafe AI's Jev][jev] is a supported hosted alternative) —
as a fast, cheap, calibrated judge.

## The problem

AI-era content volume is not digestible by humans. A researcher or engineer
following 3 arXiv categories faces ~250 papers/day; add HN, Reddit and X and
the "reading pile" becomes a guilt-generating backlog. Existing digests
(ArxivDigest, Scholar Inbox, Zotero Arxiv Daily) all profile you by **what
you say you want** (declared interests) or **what you already read**
(library history). None profile you by **what you are building right now**.

## The thesis

1. **Your agent conversations are the honest interest profile.** In the
   agent era the codebase is agent *output*; the intention lives in what
   you ask — the prompts, the corrections, the dead ends that never reach
   a commit. Conversations describe your active work better than any
   self-declared preference list — and they require zero maintenance.
2. **System One economics unlock the design.** Judging 1,750 items/week with
   a traditional LLM costs ~$59/year even with a mini model. With the
   hosted Jev API as shipped (~$17/year — the state rides every request)
   and a local System One judge it is **$0**: free-at-volume changes what
   is worth building.
3. **Calibrated confidence belongs in the UI.** The judge returns probabilities
   calibrated against outcomes. Sibilla renders low-confidence judgments as
   an explicit "you look here" zone instead of hiding uncertainty.

## What it looks like

A single self-contained HTML treemap, delivered daily (cron → Telegram):

- **Size** = relevance to your active work
- **Color** = novelty (new techniques vs. known ground)
- **Quadrants** = READ / SKIM / KILL + a gray low-confidence band
- **Cross-source dedup**: the same result surfacing on arXiv + HN + Reddit
  collapses into one node ("echo detection")

## Documentation

| Doc | Content |
|-----|---------|
| [00 – Vision](docs/00-VISION.md) | Problem, thesis, non-goals |
| [01 – Architecture](docs/01-ARCHITECTURE.md) | Components, data flow, repo layout |
| [02 – Sources](docs/02-SOURCES.md) | Source plugins & per-source JudgeConfig |
| [03 – Judge](docs/03-JUDGE.md) | Pluggable backends (CLM local / Jev hosted), state construction, primitives, cost model |
| [04 – Visual](docs/04-VISUAL.md) | Treemap spec, quadrants, confidence zone, delivery |
| [05 – Roadmap](docs/05-ROADMAP.md) | Phases, risks, validation criteria |

## Status

🚧 **Implemented (v0 → v2 code complete)** — the full pipeline per the
design docs: arXiv/HN/Reddit/X sources with per-source JudgeConfigs, the
pluggable judge (local CLM reference + hosted TypeSafe Jev), deterministic
state builder, echo dedup, single-file treemap with the human zone, tune
loop and drift view. Dogfooding per the v0 exit criteria in
[05 – Roadmap](docs/05-ROADMAP.md) starts now.

## Prerequisites

Sibilla itself is a plain Python package (`pip install -e ".[dev]"`,
Python ≥ 3.10). The judge backend has heavier requirements and is
**optional at install time** — without it the pipeline runs and degrades
loudly (stale badge) instead of silently.

**CLM backend (local, reference)** — see the
[CLM repo](https://github.com/Contrastive-LM/CLM#installation) for full
instructions:

- **vLLM** serving the `Qwen/Qwen3-8B` embedding encoder — needs an
  NVIDIA GPU (~16 GB VRAM) and a CUDA-enabled vLLM install
  ([vLLM quickstart](https://docs.vllm.ai/en/latest/getting_started/quickstart.html))
- **`clm-serve`** — `pip install contrastive-lm` (downloads the 75 MB
  reference head on first run); runs on CPU or GPU
- No GPU on this machine? See
  [GPU topology](docs/03-JUDGE.md#gpu-topology): the encoder can run on
  any GPU host in your tailnet (`--emb-url`).

**TypeSafe Jev backend (hosted alternative)** — no local infra, just an
API key from [typesafe.ai](https://typesafe.ai/); set `backend =
"typesafe"` in `sibilla.yaml` and export the key per
[03 – Judge](docs/03-JUDGE.md#backends). This is also the fallback when
you don't want to run vLLM at all.

Hardware summary: with a GPU on the machine or reachable over the
network the weekly cycle takes minutes; all-CPU works but turns it into
hours.

## Quickstart

```bash
pip install -e ".[dev]"

# 1a. run the reference judge backend (docs/03): vLLM encoder + clm-serve.
#     This needs an NVIDIA GPU. If you don't have one, skip to 1b.
vllm serve Qwen/Qwen3-8B --served-model-name qwen3-8b \
  --runner pooling --max-model-len 8192 --port 8090
clm-serve --max-tokens 8192 --port 8700

# 1b. ALTERNATIVE, no GPU/no local infra: hosted TypeSafe Jev instead.
#     Same wire API; ~$17/year at Sibilla's volume as shipped. Skip step 1a,
#     export the API key, and start from the pre-populated Jev config
#     (sibilla.yaml.example.jev — same shape as the example, judge
#     block already set: backend typesafe, api_key_env TYPESAFE_API_KEY):
export TYPESAFE_API_KEY=...                 # get one at https://typesafe.ai/
cp sibilla.yaml.example.jev sibilla.yaml    # then edit sources, judge
#     (if you go 1b, step 2's cp is already done — just edit the file)

# 2. configure
cp sibilla.yaml.example sibilla.yaml   # sources, judge (state reads your conversations)

# 3. the daily loop
sibilla run                            # fetch → normalize → state → judge → map
sibilla map --date 2026-09-27          # regenerate any day — pure function of the DB
sibilla state --history                # drift view
sibilla run --window-hours 168         # weekly recap

#    Cold start (first run): backfill arXiv only — 192h without widening
#    the real-time sources. Idempotent: the store dedups by native id and
#    the verdict cache makes any re-fetch free.
sibilla run --source-window arxiv=192

#    Windows are per-source (arXiv 96h in the example config — listing lag
#    + quiet weekends; HN/Reddit 24h). Precedence: --source-window >
#    --window-hours > per-source window: > global window:.

#    Start clean: `prune` deletes the local state (sibilla.db — items,
#    verdicts, state history, labels, calibrations) after showing you what
#    it holds; --maps also removes the generated HTML. Config and RADAR.md
#    are kept. The next `sibilla run` starts from scratch (use
#    --source-window arxiv=192 to backfill immediately).
sibilla prune            # asks for confirmation; --yes skips the prompt

# 4. tune loop: click labels in the map → export labels → fit gates
sibilla tune --import labels.json --fit

# 5. delivery (optional): channel: telegram in sibilla.yaml, then
sibilla run --deliver
# crontab: 30 6 * * *  cd ~/radar && sibilla run --deliver
```

Without a backend the pipeline degrades loudly (stale badge), never
silently. All state stays in the local SQLite (`sibilla.db`).

## License

See [LICENSE](LICENSE).

[clm]: https://github.com/Contrastive-LM/CLM
[jev]: https://docs.typesafe.ai/
