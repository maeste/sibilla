# Sibilla

> *The oracle that reads the stream so you don't have to.*

Sibilla is a personal research radar that filters the daily flood of AI/tech
content (arXiv, Hacker News, Reddit, X) through **what you actually build**,
using [TypeSafe AI's Jev](https://docs.typesafe.ai/) — a System One model —
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
   a traditional LLM costs ~$59/year even with a mini model. With Jev
   (typed answers, free output tokens, state packed once per request) the
   same workload costs **~$1.43/year**. A 42x gap changes what is worth
   building.
3. **Calibrated confidence belongs in the UI.** Jev returns probabilities
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
| [03 – Jev Judge](docs/03-JEV-JUDGE.md) | State construction, primitives, fan-out, cost model |
| [04 – Visual](docs/04-VISUAL.md) | Treemap spec, quadrants, confidence zone, delivery |
| [05 – Roadmap](docs/05-ROADMAP.md) | Phases, risks, validation criteria |

## Status

🚧 Design phase — this repository currently contains the design documents.
Implementation follows the roadmap in [05 – Roadmap](docs/05-ROADMAP.md).

## License

See [LICENSE](LICENSE).
