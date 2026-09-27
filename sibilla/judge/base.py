"""JudgeBackend protocol, question/answer types and the shared wire client.

Both backends speak the TypeSafe-compatible wire format measured against
the hosted API's openapi.json (v0.2): a request written for one replays
on the other. Endpoints:

    POST /v1/systemone  {"model", "state", "questions": {name: question}}
                        → {"answers": {name: answer}, "usage": {...}}
                        One content per request — Sibilla composes
                        "profile state + ITEM" into `state` deterministically.
    POST /v1/rank       {"model", "context", "question", "candidates"}
                        → {"probabilities": [...], "usage": {...}}  (CLM only;
                        the hosted v0.2 emulates rank via systemone)
    GET  /health        → 2xx (falls back to GET / playground/docs page)

Question shapes (type-discriminated, per the openapi spec):
    score  → {"type": "score", "instructions": ..., "criteria": ["0", ..., "10"]}
    noul   → {"type": "noul", "instructions": ...}
    choice → {"type": "choice", "instructions": ..., "criteria": {opt: opt}}

Answer shapes by primitive:
    score  → {"type": "score", "score": 7.2, "confidence": 0.87}
    noul   → {"type": "noul", "noul": 0.81}   (no confidence → margin |2p−1|)
    choice → {"type": "choice", "choice": "release", "confidence": 0.8,
              "probabilities": {...}}
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable

import requests

Primitive = Literal["score", "noul", "choice"]


class JudgeError(RuntimeError):
    """Backend failure — recorded per item by the packer, never fatal to the cycle."""


class JudgeUnavailable(JudgeError):
    """Backend down — pipeline degrades to last verdicts with a stale badge."""


@dataclass(frozen=True)
class Question:
    """One typed question (primitive) in a source's JudgeConfig."""

    id: str
    primitive: Primitive
    text: str
    scale_min: float = 0.0  # score only
    scale_max: float = 10.0  # score only
    options: tuple[str, ...] = ()  # choice only

    def to_wire(self) -> dict[str, Any]:
        """The hosted TypeSafe API v0.2 shape (POST /v1/systemone, see its
        openapi.json): questions ride as a dict keyed by name; `instructions`
        carries the text and `criteria` the taxonomy/rubric."""
        q: dict[str, Any] = {"type": self.primitive, "instructions": self.text}
        if self.primitive == "score":
            q["criteria"] = [str(int(i)) for i in range(int(self.scale_min), int(self.scale_max) + 1)]
        if self.primitive == "choice":
            q["criteria"] = {opt: opt for opt in self.options}
        return q


@dataclass(frozen=True)
class Answer:
    question_id: str
    primitive: Primitive
    value: float | str  # score → float in [min, max]; noul → prob in [0, 1]; choice → option label
    confidence: float  # 0..1 margin; NOT a calibrated probability for CLM (docs/03)
    distribution: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Ranked:
    """Per-candidate probability from ``rank()`` — the dedup similarity primitive."""

    probabilities: list[float]
    input_tokens: int = 0


def parse_answer(q: Question, payload: dict[str, Any]) -> Answer:
    """Normalize a wire answer into an Answer, enforcing the typed contract.

    Real TypeSafe v0.2 shapes (per openapi.json): noul → ``{"type": "noul",
    "noul": 0.98}`` (no confidence — a deterministic margin |2p−1| stands in,
    same semantics as CLM's top−rest margin); score → ``{"type": "score",
    "score": 1.7, "confidence": 0.9}``; choice → ``{"type": "choice",
    "choice": "release", "confidence": 0.9, "probabilities": {...}}``.
    """
    if q.primitive == "score":
        value = payload.get("score", payload.get("value"))
        if not isinstance(value, int | float):
            raise JudgeError(f"score answer for {q.id!r} lacks a numeric value: {payload!r}")
        value = max(q.scale_min, min(q.scale_max, float(value)))
        return Answer(q.id, "score", value, _conf(payload), _dist(payload))
    if q.primitive == "noul":
        prob = payload.get("noul", payload.get("probability", payload.get("value")))
        if not isinstance(prob, int | float):
            raise JudgeError(f"noul answer for {q.id!r} lacks a probability: {payload!r}")
        prob = max(0.0, min(1.0, float(prob)))
        # the wire answer carries no confidence; the |2p−1| margin is the same
        # top-vs-rest semantics the other primitives' confidences express
        return Answer(q.id, "noul", prob, _conf(payload, default=abs(2 * prob - 1)), _dist(payload))
    # choice: the model never invents structure — an option outside the taxonomy
    # falls back to the distribution argmax, else the answer is an error.
    choice = payload.get("choice")
    probs = {str(k): float(v) for k, v in (payload.get("probabilities") or {}).items()}
    if choice not in q.options:
        if probs:
            choice = max(probs, key=lambda k: probs[k])  # type: ignore[arg-type]
        else:
            raise JudgeError(f"choice answer for {q.id!r} is outside the taxonomy: {payload!r}")
    if choice not in q.options:  # argmax can still land outside if the distribution is junk
        raise JudgeError(f"choice answer for {q.id!r} resolved outside the taxonomy: {choice!r}")
    return Answer(q.id, "choice", str(choice), _conf(payload), probs)


def _conf(payload: dict[str, Any], default: float = 1.0) -> float:
    conf = payload.get("confidence", default)
    try:
        return max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        return default


def _dist(payload: dict[str, Any]) -> dict[str, float]:
    try:
        return {str(k): float(v) for k, v in (payload.get("distribution") or {}).items()}
    except (TypeError, ValueError):
        return {}


@runtime_checkable
class JudgeBackend(Protocol):
    """The narrow protocol every backend implements, regardless of vendor (docs/03)."""

    name: str

    def system_one(
        self, state: str, item: str, questions: Sequence[Question], *, temperature: float = 1.0
    ) -> dict[str, Answer]: ...

    def system_one_batch(
        self, state: str, items: Sequence[str], questions: Sequence[Question], *, temperature: float = 1.0
    ) -> list[dict[str, Answer]]: ...

    def rank(self, context: str, question: str, candidates: Sequence[str]) -> Ranked: ...

    def health(self) -> bool: ...


@dataclass
class CallRecord:
    """Raw response accounting — the store is also the cost ledger (docs/03)."""

    request_hash: str | None = None
    item_count: int = 1
    latency_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    error: str | None = None


class HttpJudgeBackend:
    """Shared client for the TypeSafe-compatible wire format.

    Subclasses set ``name``, default ``base_url``/``api_key_env`` and whether
    they pack many items per request (Jev) or fan out one request per item
    (CLM, amortized by the server-side vector cache).
    """

    name = "http"
    packs_items = False
    default_base_url = ""
    default_api_key_env: str | None = None
    requires_api_key = False  # typesafe: True — a missing key is auth, not downtime
    endpoint_system_one = "/v1/systemone"  # TypeSafe v0.2 path (their openapi.json)
    endpoint_rank: str | None = "/v1/rank"  # None → rank is emulated via systemone
    input_cost_per_mtok = 0.0  # output tokens are free on both documented backends

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str = "clm-latest",
        timeout_s: float = 30.0,
        retries: int = 2,
        session: requests.Session | None = None,
    ):
        self.base_url = (base_url or self.default_base_url).rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout_s = timeout_s
        self.retries = retries
        self.session = session or requests.Session()

    # ------------------------------------------------------------ public

    def system_one(
        self, state: str, item: str, questions: Sequence[Question], *, temperature: float = 1.0
    ) -> dict[str, Answer]:
        return self.system_one_batch(state, [item], questions, temperature=temperature)[0]

    def system_one_batch(
        self, state: str, items: Sequence[str], questions: Sequence[Question], *, temperature: float = 1.0
    ) -> list[dict[str, Answer]]:
        """The real hosted shape (TypeSafe v0.2): one content per request with
        all questions mixed in — so "batch" here is a client-side loop, and
        both backends are fan-out shapes (docs/03 updated accordingly). The
        state rides along in every request; the packer's concurrency bounds
        politeness."""
        if not items:
            return []
        wire_questions = {q.id: q.to_wire() for q in questions}
        out: list[dict[str, Answer]] = []
        for item in items:
            payload = self._post(
                self.endpoint_system_one,
                {"model": self.model, "state": self._compose(state, item), "questions": wire_questions},
            )
            answers = payload.get("answers")
            if not isinstance(answers, dict):
                raise JudgeError(f"systemone returned {type(answers).__name__} answers, expected an object")
            out.append({q.id: parse_answer(q, answers[q.id]) for q in questions if q.id in answers})
        return out

    def rank(self, context: str, question: str, candidates: Sequence[str]) -> Ranked:
        """Similarity primitive for the dedup engine (docs/03).

        CLM serves POST /v1/rank natively; the hosted API (v0.2) does not, so
        there rank is emulated with one systemone request per candidate —
        same story probability from the noul primitive. Bounded by callers:
        candidates are the dedup engine's cluster canonicals, never O(n²).
        """
        if not candidates:
            return Ranked([], 0)
        if self.endpoint_rank is not None:
            payload = self._post(
                self.endpoint_rank,
                {"model": self.model, "context": context, "question": question, "candidates": list(candidates)},
            )
            probs = payload.get("probabilities")
            if not isinstance(probs, list) or len(probs) != len(candidates):
                raise JudgeError(f"rank returned {len(probs or [])} probabilities for {len(candidates)} candidates")
            usage = payload.get("usage") or {}
            return Ranked([max(0.0, min(1.0, float(p))) for p in probs], int(usage.get("input_tokens") or 0))
        # emulation path (hosted): each candidate gets its own yes/no question
        probe = Question(id="same_story", primitive="noul", text=question)
        tokens = 0
        probs: list[float] = []
        for candidate in candidates:
            answers = self.system_one(context, candidate, [probe])
            tokens += (
                int(getattr(self, "last_call", None).input_tokens or 0) if getattr(self, "last_call", None) else 0
            )
            probs.append(float(answers[probe.id].value))
        return Ranked(probs, tokens)

    def health(self) -> bool:
        ok, _reason = self.health_detail()
        return ok

    def health_detail(self) -> tuple[bool, str]:
        """(up, reason) — "down" must say WHY: unreachable, unauthorized or key missing.

        A hosted backend whose key is absent is an auth problem, not an
        outage; the run summary and the map banner carry the distinction.
        """
        if self.requires_api_key and not self.api_key:
            env = self.default_api_key_env or "the configured api_key_env"
            return False, f"api key missing — export {env} ({self.base_url})"
        last = "no healthy endpoint"
        for path in ("/health", "/"):
            try:
                resp = self.session.get(self.base_url + path, timeout=5)
            except requests.RequestException as exc:
                return False, f"unreachable at {self.base_url} ({type(exc).__name__})"
            if resp.ok:
                return True, ""
            if resp.status_code in (401, 403):  # definitive on either path
                env = self.default_api_key_env or "the configured api_key_env"
                return False, f"auth failed (HTTP {resp.status_code}) — check {env} / key validity"
            last = f"HTTP {resp.status_code} at {self.base_url}{path}"  # try the fallback path
        return False, last

    def cost_usd(self, input_tokens: int | None) -> float:
        if input_tokens is None:
            return 0.0
        return input_tokens / 1_000_000 * self.input_cost_per_mtok

    # ----------------------------------------------------------- private

    @staticmethod
    def _compose(state: str, item: str) -> str:
        """One content field on the wire: profile state + item, deterministically."""
        return f"{state}\n\n---\n\nITEM:\n{item}".strip()

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        raw = json.dumps(body, sort_keys=True)
        last_exc: Exception | None = None
        for attempt in range(self.retries + 1):
            start = time.monotonic()
            try:
                resp = self.session.post(self.base_url + path, data=raw, headers=headers, timeout=self.timeout_s)
            except requests.RequestException as exc:
                last_exc = exc
                if not self._retryable(exc) or attempt == self.retries:
                    break
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code >= 500 and attempt < self.retries:
                last_exc = JudgeError(f"backend {resp.status_code} for {path}: {resp.text[:200]}")
                time.sleep(0.5 * (attempt + 1))
                continue
            if resp.status_code != 200:
                raise JudgeError(f"backend {resp.status_code} for {path}: {resp.text[:200]}")
            self.last_call = CallRecord(
                request_hash=None,  # filled by the packer (it owns request hashing)
                item_count=1,  # v0.2 wire: one content per request
                latency_ms=int((time.monotonic() - start) * 1000),
                input_tokens=(resp.json().get("usage") or {}).get("input_tokens"),
                output_tokens=(resp.json().get("usage") or {}).get("output_tokens"),
            )
            return resp.json()
        raise JudgeUnavailable(f"backend unreachable after {self.retries + 1} attempts: {last_exc}")

    @staticmethod
    def _retryable(exc: Exception) -> bool:
        if isinstance(exc, requests.Timeout):
            return True
        resp = getattr(exc, "response", None)
        return resp is not None and resp.status_code >= 500
