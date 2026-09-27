"""JudgeConfig registry — one config per source (docs/02), plus scoring/routing.

Composite score per source (docs/03): ``size_score = relevance`` is primary;
for arXiv the applicability question ("usable within ~3 months") is the READ
boost, folded in as ``0.7 * relevance + 0.3 * applicability``. Other sources
composite on relevance alone.

Hard routing overrides (docs/02): HN ``drama`` never enters READ; Reddit
``meme``/``question`` auto-KILL; X ``meta-commentary`` auto-KILL; on HN/X a
low ``substance`` probability is a KILL override (opinion churn, not signal).
"""

from __future__ import annotations

from typing import Any

from sibilla.judge.base import Answer, Question
from sibilla.sources.base import JudgeConfig

KILL_OVERRIDE_SUBSTANCE = 0.5  # substance prob below this → KILL regardless of score


ARXIV_CONFIG = JudgeConfig(
    source="arxiv",
    version="1",
    char_budget=4000,  # title + abstract (~350 tokens) with headroom
    questions=(
        Question("relevance", "score", "How relevant is this to the active work in the state? (0-10)", scale_max=10),
        Question(
            "applicability",
            "score",
            "Could this be applied in the state's projects within ~3 months? (0-10)",
            scale_max=10,
        ),
        Question("novelty", "noul", "Does this introduce techniques/concepts absent from the state?"),
    ),
    quadrant_thresholds={"read": 7.0, "skim": 4.0},
)

HACKERNEWS_CONFIG = JudgeConfig(
    source="hackernews",
    version="1",
    char_budget=3000,  # title + URL domain + top comments excerpt
    questions=(
        Question("relevance", "score", "How relevant is this to the active work in the state? (0-10)", scale_max=10),
        Question("substance", "noul", "Does this point to a concrete artifact/fact rather than opinion churn?"),
        Question(
            "kind",
            "choice",
            "What is this thread?",
            options=("announcement", "benchmark", "incident", "opinion", "drama"),
        ),
    ),
    quadrant_thresholds={"read": 7.0, "skim": 4.0},
)

REDDIT_CONFIG = JudgeConfig(
    source="reddit",
    version="1",
    char_budget=3000,  # title + selftext excerpt
    questions=(
        Question("relevance", "score", "How relevant is this to the active work in the state? (0-10)", scale_max=10),
        Question("novelty", "noul", "Does this introduce techniques/concepts absent from the state?"),
        Question(
            "kind",
            "choice",
            "What kind of post is this?",
            options=("tooling", "experience-report", "question", "meme"),
        ),
    ),
    quadrant_thresholds={"read": 7.0, "skim": 4.0},
)

X_CONFIG = JudgeConfig(
    source="x",
    version="1",
    char_budget=1000,  # tweet text + quoted-tweet/article title
    questions=(
        Question("relevance", "score", "How relevant is this to the active work in the state? (0-10)", scale_max=10),
        Question("substance", "noul", "Does this make a verifiable claim or point to an artifact, vs. vibes?"),
        Question(
            "kind",
            "choice",
            "What kind of post is this?",
            options=("release", "research", "opinion", "meta-commentary"),
        ),
    ),
    # Aggressive cuts (docs/02): on X, KILL should dominate (~90%).
    quadrant_thresholds={"read": 8.0, "skim": 6.0},
)

_REGISTRY: dict[str, JudgeConfig] = {
    ARXIV_CONFIG.source: ARXIV_CONFIG,
    HACKERNEWS_CONFIG.source: HACKERNEWS_CONFIG,
    REDDIT_CONFIG.source: REDDIT_CONFIG,
    X_CONFIG.source: X_CONFIG,
}


def get_config(source: str) -> JudgeConfig:
    try:
        return _REGISTRY[source]
    except KeyError:
        raise KeyError(f"no JudgeConfig registered for source {source!r}") from None


def known_sources() -> list[str]:
    return list(_REGISTRY)


def composite_score(source: str, answers: dict[str, Answer]) -> float:
    """Source composite on the 0-10 scale (see module docstring)."""
    relevance = _num(answers.get("relevance"), 0.0)
    if source == "arxiv":
        applicability = _num(answers.get("applicability"), 0.0)
        return 0.7 * relevance + 0.3 * applicability
    return relevance


def size_score(source: str, answers: dict[str, Answer]) -> float:
    """Treemap area driver: relevance, primary by design (docs/03 primitives)."""
    return _num(answers.get("relevance"), 0.0)


def novelty(source: str, answers: dict[str, Answer]) -> float:
    """Treemap color driver: new-to-you probability (docs/04 encoding)."""
    a = answers.get("novelty")
    if a is not None:
        return _num(a, 0.0)
    # HN/X carry `substance` (concrete artifact vs. opinion churn) instead of a
    # novelty primitive; the color channel needs a deterministic proxy — low
    # substance ⇒ known opinion ground, high substance ⇒ potentially new ground.
    substance = answers.get("substance")
    if substance is not None:
        return 1.0 - _num(substance, 0.0)
    return 1.0 - min(1.0, _num(answers.get("relevance"), 0.0) / 10.0)


def min_confidence(answers: dict[str, Answer]) -> float:
    if not answers:
        return 0.0
    return min(a.confidence for a in answers.values())


def classify(source: str, answers: dict[str, Answer]) -> tuple[str, str]:
    """Return ``(quadrant, reason)`` applying thresholds then hard overrides.

    Routing rules are evaluated in a fixed order so placement is a pure
    function of the answers (deterministic ground truth, docs/01 rule 1).
    """
    cfg = get_config(source)
    composite = composite_score(source, answers)
    if composite >= cfg.read_threshold:
        quadrant, reason = "READ", f"composite {composite:.1f} ≥ {cfg.read_threshold:g}"
    elif composite >= cfg.skim_threshold:
        quadrant, reason = "SKIM", f"composite {composite:.1f} ≥ {cfg.skim_threshold:g}"
    else:
        quadrant, reason = "KILL", f"composite {composite:.1f} < {cfg.skim_threshold:g}"

    kind = answers.get("kind")
    if kind is not None and isinstance(kind.value, str):
        if source == "hackernews" and kind.value == "drama":
            return "KILL", "kind=drama — never READ regardless of score"
        if source == "reddit" and kind.value in ("meme", "question"):
            return "KILL", f"kind={kind.value} — auto-KILL"
        if source == "x" and kind.value == "meta-commentary":
            return "KILL", "kind=meta-commentary — auto-KILL"

    substance = answers.get("substance")
    if substance is not None and _num(substance, 1.0) < KILL_OVERRIDE_SUBSTANCE and quadrant == "READ":
        return "KILL", "substance below 0.5 — opinion churn override"
    return quadrant, reason


def _num(answer: Answer | None, default: float) -> float:
    if answer is None or isinstance(answer.value, str):
        return default
    return float(answer.value)


def answers_to_scores(source: str, answers: dict[str, Answer]) -> dict[str, Any]:
    """JSON-able scores for the verdicts row: per-question value + composite."""
    scores: dict[str, Any] = {qid: a.value for qid, a in answers.items()}
    scores["composite"] = round(composite_score(source, answers), 3)
    return scores


def answers_to_confidences(answers: dict[str, Answer]) -> dict[str, float]:
    return {qid: round(a.confidence, 3) for qid, a in answers.items()}
