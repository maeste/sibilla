"""Shared fixtures: tmp store, deterministic fake backend, stub sources."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timezone

import pytest

from sibilla.config import Config
from sibilla.judge.base import Answer, Ranked
from sibilla.judge.configs import get_config
from sibilla.judge.packer import JudgeJob, Packer
from sibilla.sources.base import RawItem
from sibilla.store import ItemRow, Store


class FakeBackend:
    """Deterministic System One stand-in for tests — no network, no GPU.

    Answers derive from keyword rules so placement is controllable per test;
    every call is recorded for ledger assertions.
    """

    name = "fake"
    packs_items = False
    model = "fake-latest"

    def __init__(
        self,
        relevance: float = 8.0,
        novelty: float = 0.9,
        confidence: float = 0.95,
        kind: str | None = None,
        fail_ids: set[str] | None = None,
        packs: bool = False,
    ):
        self.relevance = relevance
        self.novelty = novelty
        self.confidence = confidence
        self.kind = kind
        self.fail_ids = fail_ids or set()
        self.packs_items = packs
        self.calls: list[dict] = []
        self.last_call = None

    def system_one(self, state, item, questions: Sequence, *, temperature: float = 1.0):
        return self.system_one_batch(state, [item], questions, temperature=temperature)[0]

    def system_one_batch(self, state, items, questions: Sequence, *, temperature: float = 1.0):
        self.calls.append({"state_len": len(state), "items": len(items)})
        out = []
        for text in items:
            if any(f in text for f in self.fail_ids):
                from sibilla.judge.base import JudgeError

                raise JudgeError(f"poisoned item: {text[:40]}")
            answers = {}
            for q in questions:
                if q.primitive == "score":
                    value = self.relevance if q.id == "relevance" else max(0.0, self.relevance - 1.0)
                    answers[q.id] = Answer(q.id, "score", value, self.confidence)
                elif q.primitive == "noul":
                    answers[q.id] = Answer(q.id, "noul", self.novelty, self.confidence)
                else:
                    answers[q.id] = Answer(q.id, "choice", self.kind or q.options[0], self.confidence)
            out.append(answers)
        return out

    def rank(self, context: str, question: str, candidates: Sequence[str]) -> Ranked:
        # deterministic stand-in: same story when a distinctive word is shared
        def tokens(text: str) -> set[str]:
            return {w for w in "".join(c if c.isalnum() else " " for c in text.lower()).split() if len(w) >= 5}

        ctx = tokens(context)
        probs = [1.0 if tokens(c) & ctx else 0.05 for c in candidates]
        return Ranked(probs, 100)

    def health(self) -> bool:
        return True

    def cost_usd(self, input_tokens):
        return 0.0


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "test.db")
    yield s
    s.close()


@pytest.fixture
def fake_backend():
    return FakeBackend()


@pytest.fixture
def sample_items() -> list[ItemRow]:
    ts = int(datetime(2026, 9, 27, 5, 0, tzinfo=timezone.utc).timestamp())
    return [
        ItemRow(
            id="arxiv:2609.00001",
            source="arxiv",
            native_id="2609.00001",
            fetched_at=ts,
            title="A better attention mechanism",
            url="https://arxiv.org/abs/2609.00001",
            author="A. Author",
            published=ts,
            meta={"arxiv_base_id": "2609.00001"},
            body="A better attention mechanism\n\nWe propose a sparse attention variant.",
        ),
        ItemRow(
            id="hackernews:1",
            source="hackernews",
            native_id="1",
            fetched_at=ts,
            title="Show HN: A better attention mechanism",
            url="https://arxiv.org/abs/2609.00001",
            author="hnuser",
            published=ts,
            meta={"points": 120},
            body="Show HN: discussion of the same sparse attention paper.",
        ),
        ItemRow(
            id="reddit:abc",
            source="reddit",
            native_id="abc",
            fetched_at=ts,
            title="LocalLLaMA: sparse attention in practice",
            url="https://reddit.com/r/LocalLLaMA/abc",
            author="ruser",
            published=ts,
            meta={"ups": 80},
            body="LocalLLaMA: sparse attention in practice\n\nTried it, works.",
        ),
    ]


def make_job(source: str, items: list[ItemRow]) -> JudgeJob:
    job = JudgeJob(source=source, config=get_config(source))
    for i in items:
        job.add(i.id, i.body)
    return job


def run_packer(backend, state: str, job: JudgeJob):
    return Packer(backend, ledger=lambda **kw: None).run(state, [job])


@pytest.fixture
def sample_raw_items() -> dict[str, list[RawItem]]:
    """One arXiv paper echoed by HN (same arXiv URL) + a standalone reddit post."""
    from datetime import timedelta

    arxiv_id = "2609.77777"
    now = datetime.now(timezone.utc)

    def ts(hours_ago: float) -> datetime:
        return now - timedelta(hours=hours_ago)

    return {
        "arxiv": [
            RawItem(
                source="arxiv",
                native_id=f"{arxiv_id}v1",
                title="Sparse attention revisited",
                url=f"https://arxiv.org/abs/{arxiv_id}v1",
                author="A. Author",
                published=ts(2),
                body="Sparse attention revisited\n\nWe cut KV memory by 10x.",
            )
        ],
        "hackernews": [
            RawItem(
                source="hackernews",
                native_id="999",
                title="Sparse attention revisited (thread)",
                url=f"https://arxiv.org/abs/{arxiv_id}",
                author="hnuser",
                published=ts(1),
                body="Discussion of the same sparse attention paper.",
            )
        ],
        "reddit": [
            RawItem(
                source="reddit",
                native_id="r1",
                title="LocalLLaMA: tried sparse attention",
                url="https://www.reddit.com/r/LocalLLaMA/comments/r1/",
                author="ruser",
                published=ts(1),
                body="Tried it on a 7B model, works.",
            )
        ],
    }


# ------------------------------------------------------- pipeline fixtures


class StubSource:
    name = "stub"

    def __init__(self, items=None, error: Exception | None = None):
        self.items = items or []
        self.error = error

    def fetch(self, since):
        if self.error:
            raise self.error
        return self.items


class FakeState:
    def __init__(self, state_hash="deadbeef" * 8, text="ACTIVE WORK PROFILE (fake)"):
        self.state_hash = state_hash
        self.text = text
        self.conversations = []
        self.radar_md_used = False


@pytest.fixture
def pipeline_env(tmp_path, monkeypatch, sample_raw_items):
    """Stubbed sources + fake backend + fake state around a tmp store."""
    cfg = Config(db_path=str(tmp_path / "p.db"), output_dir=str(tmp_path))
    registry = {
        "arxiv": lambda cfg: StubSource(sample_raw_items["arxiv"]),
        "hackernews": lambda cfg: StubSource(sample_raw_items["hackernews"]),
        "reddit": lambda cfg: StubSource(sample_raw_items["reddit"]),
        "x": lambda cfg: StubSource([]),
    }
    monkeypatch.setattr("sibilla.pipeline.sources_registry", lambda: registry)
    monkeypatch.setattr("sibilla.pipeline.make_backend", lambda cfg: FakeBackend())

    from sibilla.state import builder as state_builder

    monkeypatch.setattr(state_builder, "build_state", lambda *a, **kw: FakeState("cafe" * 16))
    return cfg
