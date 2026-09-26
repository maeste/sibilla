# 05 — Roadmap

**Project:** Sibilla
**Depends on:** all design docs

## Phases

### v0 — "Oracle solo" (target: 2 weekends)

Scope: one source, full loop, daily dogfood.

- [ ] `store.py` — SQLite schema (items, verdicts, state_versions)
- [ ] `state/builder.py` — GitHub → state text + `state_hash`
  (gh CLI or REST; RADAR.md appended verbatim)
- [ ] `sources/arxiv.py` — Atom fetch, 3 categories, 24h window
- [ ] `judge/` — backend protocol + CLM backend (local) + packer + arXiv JudgeConfig
  (relevance / applicability / novelty)
- [ ] `render/` — single-file treemap, quadrants + human zone v0
- [ ] `cli.py` — `sibilla run`, `sibilla map`
- [ ] Manual delivery (open the HTML) — cron integration comes later

**Exit criteria:** 7 consecutive days of personal use; ≥1 item/week that
you'd have missed otherwise; KILL trust (no more than ~1 manual
`revive`/day).

### v0.5 — Judgment hardening (+1 weekend)

- [ ] Confidence band tuning against your own accept/reject behavior
- [ ] Question wording A/B via JudgeConfig versions (diff quadrants on
  same state — the cache makes this free-ish)
- [ ] Cost/latency ledger surfaced in map header

**Exit criteria:** READ precision (of items you actually read) ≥ 60%.

### v1 — Multi-source + delivery (+2 weekends)

- [ ] `sources/hackernews.py` (Algolia) + its JudgeConfig (substance /
  kind, drama-KILL)
- [ ] `sources/reddit.py` (public JSON) + its JudgeConfig
- [ ] Dedup engine v1 — deterministic keys only (URL/arXiv-id/repo name)
- [ ] Hermes cron → Telegram daily 06:30 + weekly recap

**Exit criteria:** 14 days running unattended; cross-source echo visible
≥ 3×/week.

### v2 — X source + semantic dedup (scope check before build)

- [ ] X transport decision with real numbers (official API vs bridge vs
  export) — **go/no-go gate**, see risks
- [ ] Monitored-profile list config
- [ ] X JudgeConfig (substance / kind; aggressive ~90% KILL target)
- [ ] Semantic dedup residual: judge `rank()` clustering on candidate pairs
- [ ] Weekly drift view: state_hash history vs quadrant shifts

## Risks & mitigations

| # | Risk | Sev | Mitigation |
|---|------|-----|-----------|
| R1 | X API pricing ($200/mo class) kills the v2 source | 🔴 | Pluggable transport decided at v2 gate; X is additive, never load-bearing; monitored-profile list bounds volume |
| R2 | Hosted-judge rate limits "adjusting dynamically" (TypeSafe's words) | 🟡 | Local CLM makes this moot; for the hosted backend we are ~10 req/week — three orders under limit; retries + degrade-to-last-verdicts |
| R3 | Profile quality: repos too heterogeneous → mushy state | 🟡 | RADAR.md manual override; monthly rebuild diff to inspect drift |
| R4 | Calibration in practice ≠ docs promise | 🟡 | v0.5 measures READ precision manually; human-zone cap bounds the blast radius |
| R5 | arXiv Atom API changes / throttling | 🟢 | Trivial volumes; cached raw responses; 24h retry window |
| R6 | Scope creep toward "read-it-later / recommender / summarizer" | 🔴 | Non-goals doc (00) is the contract; every feature request must map to "filter better" or be rejected |
| R7 | Session JSONL-like format churn (n/a — no agent traces in scope) | 🟢 | Out of scope by design |

## Validation criteria (the honest list)

The project is worth continuing only if, after v1:

1. **Daily-open test:** you open the treemap most mornings unprompted.
2. **KILL trust:** ignoring the KILL area feels safe (≤1 false-negative
   revive/day).
3. **Serendipity rate:** ≥1 applicability-relevant discovery/week that
   organic channels would have delivered later or never.
4. **Cost floor:** judge spend is zero (local CLM); hosted-Jev spend < cost of one coffee/quarter.

If (1) or (2) fail after threshold tuning → the filter thesis is wrong
for this user; stop, write the post-mortem, keep the state-builder (it is
reusable as "profile-as-code" for other tools).

## Explicitly deferred

- LLM-generated digests/prose summaries (violates the System One purity
  of the design; revisit only if the treemap proves insufficient)
- Browser extension / real-time push (the daily cadence is the product)
- Multi-user / hosted version (non-goal by charter)
- Web UI beyond the single HTML file (no server is a feature)
