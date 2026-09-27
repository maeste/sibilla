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

1. **Your codebase is the honest interest profile.** Repos' topics, READMEs
   and recent commits describe your active work better than any
   self-declared preference list — and it requires zero maintenance.
2. **System One economics unlock the design.** Judging 1,750 items/week with
   a traditional LLM costs ~$59/year even with a mini model. With a
   local System One judge it is free; with hosted Jev
   (typed answers, free output tokens, state packed once per request) the
   same workload costs **~$1.43/year**. A 42x gap changes what is worth
   building.
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

## Quickstart

```bash
pip install -e ".[dev]"

# 1. run the reference judge backend (docs/03): vLLM encoder + clm-serve
vllm serve Qwen/Qwen3-8B --served-model-name qwen3-8b \
  --runner pooling --max-model-len 8192 --port 8090
clm-serve --max-tokens 8192 --port 8700

# 2. configure
cp sibilla.yaml.example sibilla.yaml   # set github_users, sources, judge
export GITHUB_TOKEN=...                # optional, avoids the 60 req/h limit

# 3. the daily loop
sibilla run                            # fetch → normalize → state → judge → map
sibilla map --date 2026-09-27          # regenerate any day — pure function of the DB
sibilla state --history                # drift view
sibilla run --window-hours 168         # weekly recap

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
