# 04 — Visual

**Project:** Sibilla
**Depends on:** [03 – Jev Judge](03-JEV-JUDGE.md)

## Design principle

The visual is the product. disk-tree's genius is not the treemap
algorithm — it is that **the next action is obvious from the geometry**:
biggest rectangle = biggest problem = delete first. Sibilla's translation:

> Biggest trusted rectangle = biggest relevance to your work = read first.
> Most of the canvas = KILL = ignore guilt-free.

## Layout

Single-file HTML (`sibemap-<date>.html`), inline JS/CSS, d3-hierarchy +
d3-treemap, no network calls at view time (links only).

```
┌───────────────────────────────────────────────────────────────────┐
│ SIBILLA — 2026-09-26   state:2f9a1c  sources: arxiv hn reddit     │
│ 1,842 items · 12 clusters · $0.031 spent this cycle               │
├───────────────────────────────┬───────────────────────────────────┤
│                               │                                   │
│   READ  (composite ≥ 7)       │   SKIM  (4–7)                     │
│   size = relevance             │   same encoding, muted palette    │
│   color = novelty              │                                   │
│                               │                                   │
├───────────────────────────────┼───────────────────────────────────┤
│  ██ low-confidence band ████  │   KILL  (< 4)                      │
│  "the oracle isn't sure —     │   ~80% of area by design;          │
│   you look here"              │   collapsed labels, click to peek  │
│  (gray, confidence < 0.5)     │                                   │
└───────────────────────────────┴───────────────────────────────────┘
```

## Encoding

| Channel | Maps to | Detail |
|---|---|---|
| Rectangle area | `relevance` score | Within-quadrant proportional layout |
| Fill hue | `novelty` (noul) | Cool = known ground → hot = new-to-you |
| Border | confidence | Dashed 0.5–0.8; gray band < 0.5 |
| Badge row | source set | arXiv/HN/Reddit/X chips per cluster |
| ⚡ marker | cluster echo | Cross-source dedup hit ("seen 3 places") |

**Clusters** (dedup output) render as one node: the richest body wins
(arXiv paper > HN thread > tweet), sibling sources become badges. An echo
with ≥2 sources gets a mild area boost — multi-source convergence is
itself a relevance signal, computed deterministically.

**Tooltip** (hover/click): title, authors/handle, one-line source excerpt,
exact scores with confidences, all mirror links. No generated prose —
Sibilla filters, it does not summarize.

## KILL quadrant discipline

- KILL occupies most of the canvas **by design** — the product promise is
  "you may ignore this much, guilt-free".
- KILL labels are collapsed (first letters only) to keep visual noise low;
  expandable on click, filterable by source.
- KILL is *reversible for a day*: `sibilla revive <item>` re-surfaces an
  item (telemetry for false negatives — feeds threshold tuning).

## The human zone

Low-confidence items (< 0.5) never enter KILL statistics and never get
fully hidden. They land in a bounded gray band sized to ~10% of the
viewport — capped because "model unsure" must not become "wall of work".
Overflow pages through (10 items/page) rather than growing.

This is the honest-UI translation of Jev's calibration: uncertainty is
surfaced, not smoothed over. No existing digest does this; it is the
visible signature of the System One requirement.

## Daily flow

```
cron (Hermes) 06:30
  └─ sibilla run          # fetch → normalize → judge → map build
  └─ sibilla map --deliver telegram
       └─ Home channel: "Sibilla · 2026-09-26 · 11 READ · 23 SKIM · 1,808 kill"
          [open treemap]  ← HTML file attached / linked
```

- Delivery: Telegram file (HTML renders in-browser on tap).
- Weekly recap (Monday): same treemap for the week + KILL-area trend +
  spend ledger line ("$0.19 this week").
- `sibilla map --date 2026-09-20` regenerates any past day from the
  store — the map is a pure function of the DB.

## Accessibility & degenerate cases

- Color encoding duplicated in border pattern (novelty) and label glyph —
  never color-only.
- Empty day (no items): explicit "quiet day" card, not a blank page.
- All-KILL day: KILL collapses to a summary line; canvas shows READ/SKIM
  emptiness honestly ("nothing mattered today").
- Source outage: header banner "reddit: unreachable — map covers 2/3
  sources"; missing source never silently shrinks the world.
