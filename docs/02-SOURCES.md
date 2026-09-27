# 02 — Sources

**Project:** Sibilla
**Depends on:** [01 – Architecture](01-ARCHITECTURE.md)

## The SourcePlugin contract

Every source is a plugin implementing:

```python
class SourcePlugin(Protocol):
    name: str                      # "arxiv" | "hn" | "reddit" | "x"
    def fetch(self, since: datetime) -> list[RawItem]: ...
    judge_config: JudgeConfig      # THIS source's question set
```

`JudgeConfig` is the architectural answer to a simple fact: **an arXiv
abstract and a tweet are different species of content.** The state is
shared and unique; the judgment is local to the source. Scales, question
wording, thresholds and quadrant cuts are all per-source.

```python
@dataclass(frozen=True)
class JudgeConfig:
    version: str
    char_budget: int                       # max chars per item sent to the judge
    questions: list[Question]              # primitives (see 03)
    quadrant_thresholds: dict[str, float]  # READ/SKIM/KILL cuts on the
                                           #   source's composite score
```

## Source specs

### `arxiv` — v0

- **Fetch:** arXiv Atom API, categories from config (default cs.AI, cs.CL,
  cs.LG), window = per-source (96h in the example config — see
  [Source config](#source-config-file-draft): arXiv's listing lag and quiet
  weekends make 24h lossy). Respect 1 req / 3s etiquette (volumes here are
  trivial).
- **Item body:** title + abstract (~350 tokens).
- **JudgeConfig focus:** substance. Long-form, low-noise input.
  - `score relevance 0–10` — applies to the active work in state?
  - `score applicability 0–10` — usable in state's projects within ~3 months?
  - `noul novelty` — introduces techniques/concepts absent from state?
- **Quadrant cuts:** READ ≥ 7 composite, SKIM ≥ 4, else KILL.

### `hackernews` — v1

- **Fetch:** Algolia Search API (`hn.algolia.com/api/v1/search_by_date`),
  front-page + ask/show filters, min score/points threshold to pre-cut
  noise deterministically.
- **Item body:** title + URL domain + top comments excerpt (char-budgeted).
- **JudgeConfig focus:** signal vs. discourse. Threads are discussions
  *about* things; the question is whether the underlying thing matters.
  - `score relevance 0–10`
  - `noul substance` — points to a concrete artifact/fact vs. opinion churn?
  - `choice stage {announcement, benchmark, incident, opinion, drama}` —
    drama never enters READ regardless of score.

### `reddit` — v1

- **Fetch:** public JSON endpoints (`/.json` suffix), subreddit list from
  config (default r/LocalLLaMA, r/MachineLearning, r/agents), min-upvote
  pre-filter.
- **Item body:** title + selftext excerpt.
- **JudgeConfig focus:** community signal. Reddit surfaces practitioner
  experience (gotchas, regressions, "don't bother with X").
  - `score relevance 0–10`
  - `choice kind {tooling, experience-report, question, meme}` — memes and
    questions auto-KILL.
  - `noul novelty`

### `x` (Twitter) — v2

- **Fetch:** monitored **profile list** from config (`x_profiles: [...]` —
  the accounts *you* chose to follow), via X API v2 timed-line endpoints.
  Content nature: short, fast, hype-prone.
  - ⚠️ X API pricing is the real constraint (Basic tier ≈ $200/mo, read-
    limited). Design must keep a pluggable transport: official API, RSS
    bridges (e.g. self-hosted RSSHub / nitter-class mirrors where legal and
    reliable), or manual export. The plugin interface isolates this choice;
    a decision is deferred to v2 kickoff with real numbers.
- **Item body:** tweet text + quoted-tweet/linked-article title if present.
- **JudgeConfig focus:** hype filtering. The X-specific questions are the
  clearest example of why JudgeConfig is per-source:
  - `score relevance 0–10`
  - `noul substance` — verifiable claim/artifact vs. vibes?
  - `choice kind {release, research, opinion, meta-commentary}` —
    meta-commentary ("thoughts on the discourse") auto-KILL.
  - Thresholds tuned more aggressively: on X, KILL should dominate (~90%).

## Cross-source dedup (the echo engine)

The same fact typically surfaces as: arXiv paper → HN thread → Reddit
crosspost → X hot take. Four items, one signal. The dedup engine (runs
after judging, before map build):

1. **Deterministic keys first:** normalized URL (strip UTM), arXiv ID
   regex, GitHub repo full name. Catches most echoes at zero cost.
2. **Semantic residual:** for items sharing a deterministic key OR scoring
   READ/SKIM, a judge `noul`-style pairwise question — "do these two items
   report the same underlying artifact/result?" — clusters the rest.
   Pairwise on candidates only, not O(n²) on everything.
3. **Cluster node** inherits `max(scores)` and the richest body (paper >
   thread > tweet), keeps all source links as evidence badges.

This is the recycled engine from the issue-clustering idea (idea C):
"cluster similar items + verdict" is a general Sibilla capability, not a
per-source hack.

## Source config file (draft)

```yaml
# sibilla.yaml
window: 24                # default window (hours) for sources without their own
sources:
  arxiv:
    categories: [cs.AI, cs.CL, cs.LG]
    window: 96            # arXiv lists with a lag and goes quiet on weekends —
                          # a 24h fetch would miss anything listed late;
                          # wider is safe (dedup by native id, verdict cache)
  hackernews:
    min_points: 40
    window: 24
  reddit:
    subreddits: [LocalLLaMA, MachineLearning, agents]
    min_upvotes: 50
    window: 24
  x:                        # v2
    profiles: [karpathy, swyx, ...]
    transport: auto         # api | bridge | export
state:
  github_users: [maeste]
  github_orgs: [RisorseArtificiali]
  radar_md: RADAR.md
delivery:
  channel: telegram
```

Each source can be disabled independently; the pipeline never hard-depends
on any single source being reachable.

**Windows are per-source.** Fetch and map coverage resolve per source:
`--source-window name=hours` (CLI) > `--window-hours` (CLI, global) >
`window:` in the source block > the global `window:`. A cold start
backfills arXiv only, without widening the real-time sources:

```bash
sibilla run --source-window arxiv=192
```
