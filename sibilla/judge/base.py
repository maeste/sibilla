"""JudgeBackend protocol, question/answer types and the shared wire client.

Both backends speak the same TypeSafe-compatible wire format (docs/03):
a request written for one replays on the other. Endpoints:

    POST /v1/system_one  {"model", "state", "items", "questions", "temperature"}
                         → {"answers": [ {qid: {...}}, ... ], "usage": {...}}
    POST /v1/rank        {"model", "context", "question", "candidates"}
                         → {"probabilities": [...], "usage": {...}}
    GET  /health         → 2xx (falls back to GET / playground page)

Answer shapes by primitive:
    score  → {"value": 7.2, "confidence": 0.87, "distribution": {...}}
    noul   → {"probability": 0.81, "confidence": 0.9}
    choice → {"choice": "release", "probabilities": {...}, "confidence": 0.8}
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
        q: dict[str, Any] = {"id": self.id, "type": self.primitive, "question": self.text}
        if self.primitive == "score":
            q["min"], q["max"] = self.scale_min, self.scale_max
        if self.primitive == "choice":
            q["options"] = list(self.options)
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
    """Normalize a wire answer into an Answer, enforcing the typed contract."""
    if q.primitive == "score":
        value = payload.get("value", payload.get("score"))
        if not isinstance(value, int | float):
            raise JudgeError(f"score answer for {q.id!r} lacks a numeric value: {payload!r}")
        value = max(q.scale_min, min(q.scale_max, float(value)))
        return Answer(q.id, "score", value, _conf(payload), _dist(payload))
    if q.primitive == "noul":
        prob = payload.get("probability", payload.get("value", payload.get("p")))
        if not isinstance(prob, int | float):
            raise JudgeError(f"noul answer for {q.id!r} lacks a probability: {payload!r}")
        return Answer(q.id, "noul", max(0.0, min(1.0, float(prob))), _conf(payload), _dist(payload))
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


def _conf(payload: dict[str, Any]) -> float:
    conf = payload.get("confidence", 1.0)
    try:
        return max(0.0, min(1.0, float(conf)))
    except (TypeError, ValueError):
        return 1.0


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
        if not items:
            return []
        payload = self._post(
            "/v1/system_one",
            {
                "model": self.model,
                "state": state,
                "items": list(items),
                "questions": [q.to_wire() for q in questions],
                "temperature": temperature,
            },
        )
        answers = payload.get("answers")
        if not isinstance(answers, list) or len(answers) != len(items):
            raise JudgeError(f"system_one returned {type(answers).__name__} for {len(items)} items")
        return [
            {q.id: parse_answer(q, per_item[q.id]) for q in questions if q.id in per_item}
            for per_item in ({a: v for a, v in item_answers.items()} for item_answers in answers)
        ]

    def rank(self, context: str, question: str, candidates: Sequence[str]) -> Ranked:
        if not candidates:
            return Ranked([], 0)
        payload = self._post(
            "/v1/rank",
            {"model": self.model, "context": context, "question": question, "candidates": list(candidates)},
        )
        probs = payload.get("probabilities")
        if not isinstance(probs, list) or len(probs) != len(candidates):
            raise JudgeError(f"rank returned {len(probs or [])} probabilities for {len(candidates)} candidates")
        usage = payload.get("usage") or {}
        return Ranked([max(0.0, min(1.0, float(p))) for p in probs], int(usage.get("input_tokens") or 0))

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
                item_count=len(body.get("items", [1])),
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
