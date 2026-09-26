# 00 — Vision

**Project:** Sibilla
**Status:** Design phase
**Last updated:** 2026-09-26

## The problem, precisely

The AI era produces content faster than any human can digest. This is not a
metaphor, it is arithmetic:

- 3 arXiv categories (cs.AI, cs.CL, cs.LG) ≈ **250 papers/day**
- Hacker News front page ≈ 30–40 substantive threads/day
- A monitored list of 20–30 X accounts produces hundreds of items/day

The consequence is a **knowledge debt** problem, symmetric to technical
debt: important signals (a technique you could apply next month, a
competitor shift, a security issue in a dependency) drown in noise, and the
reading pile becomes a source of guilt rather than a tool.

## Why existing tools don't solve it

| Tool | Profile source | Judgment | Output |
|------|---------------|----------|--------|
| Scholar Inbox | Explicit ratings + active learning | Proprietary recommender | Web feed |
| AutoLLM/ArxivDigest | Declared interests (YAML) | GPT rating | Email HTML |
| arxivdigest.org | Declared interests | Collaborative filtering | Email |
| Zotero Arxiv Daily | Reading history (Zotero lib) | LLM rating | Email |

Two structural gaps:

1. **All profile declared or historical preferences.** None derive the
   profile from *current work*. Declared interests rot; reading history is
   biased toward what you already know. The code you committed this month
   is the most honest, self-updating profile you own.
2. **All output text lists.** A sorted list answers "what's most relevant"
   but not "what can I safely ignore" — which is the actual daily question.
   Proportional space (treemap) makes the kill decision visually obvious,
   the same way `disktree` does for disk usage.

## The thesis

**Sibilla = your repos as interest profile + a System One model as judge +
a proportional-space visual as interface.**

Three pillars:

### 1. State from code, not declarations

The judgment *state* (the judge's input) is built deterministically from GitHub:
repo topics, README lead sections, recent commit messages. An optional
`RADAR.md` per-user override covers interests invisible from code.
Deterministic construction → the model never invents the profile.

### 2. System One economics

The judge evaluates a state against typed questions (choice / score / noul) and
returns **calibrated probabilities**, not generated text. Consequences:

- **Cost**: state is packed once per request; questions ride along in
  parallel. Full workload ≈ **$1.43/year** (see
  [03 – Judge](03-JUDGE.md#cost-model)). A mini-LLM doing the same
  per-item prompting costs ~42x more.
- **Calibration**: confidence is a first-class output. Low confidence →
  surface to the human instead of guessing.
- **Typed outputs**: no parsing generated text; primitives map directly to
  treemap encoding.

### 3. Proportional-space decision surface

The interface is a treemap where **size = relevance, color = novelty**, cut
into READ / SKIM / KILL quadrants with an explicit low-confidence band.
"Delete the biggest file" becomes "read the biggest trusted rectangle."

## Non-goals

- ❌ **Not a recommender social network.** Single user (you), local state,
  no accounts, no sharing unless you export the HTML.
- ❌ **Not a reading tracker.** No read/unread state machine, no gamified
  streaks. Sibilla filters; your existing tools read.
- ❌ **Not a search engine.** No "ask questions about papers" — that job
  belongs to LLMs with retrieval, and doing it here would double the scope.
- ❌ **No summarization.** The judge does not generate text by design; the
  treemap shows titles + links + scores. Digest prose is out of scope for
  v0 (revisit in v2, see roadmap).

## Success looks like

- The daily treemap is opened **every morning** (dogfooding is the only
  real metric for a personal tool).
- KILL quadrant ≥ 80% of items by area, and you trust it.
- You learn about at least one applicability-relevant item/week *before*
  it would have reached you organically.
