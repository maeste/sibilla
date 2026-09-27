"""Fan-out scheduling — abstracts batch (Jev) and fan-out (CLM) shapes (docs/03).

Scheduling rules, both backends:
- items are grouped per source (a request never mixes JudgeConfigs);
- per-source char budgets truncate deterministically before the backend sees
  the item (sources/base.truncate_to_budget);
- every response is persisted raw (request hash, latency, usage) — the store
  is also the cost ledger;
- failure isolation: one poisoned item cannot take down the cycle.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field

from sibilla.judge.base import Answer, JudgeError, JudgeUnavailable
from sibilla.sources.base import JudgeConfig, truncate_to_budget

LedgerFn = Callable[..., None]  # store.save_judge_call signature


@dataclass
class JudgeJob:
    """All items of one source, already truncated to the source char budget."""

    source: str
    config: JudgeConfig
    item_ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)
    truncated_flags: list[bool] = field(default_factory=list)

    def add(self, item_id: str, text: str) -> None:
        cut, truncated = truncate_to_budget(text, self.config.char_budget)
        self.item_ids.append(item_id)
        self.texts.append(cut)
        self.truncated_flags.append(truncated)


@dataclass
class JudgeResult:
    answers_by_item: dict[str, dict[str, Answer]] = field(default_factory=dict)
    truncated_by_item: dict[str, bool] = field(default_factory=dict)
    errors_by_item: dict[str, str] = field(default_factory=dict)

    @property
    def ok(self) -> int:
        return len(self.answers_by_item)


class Packer:
    """Runs JudgeJobs against a backend, whichever scheduling shape it uses."""

    def __init__(
        self,
        backend: object,
        ledger: LedgerFn,
        concurrency: int = 4,
        pack_size: int = 170,
        temperature: float = 1.0,
    ):
        self.backend = backend
        self.ledger = ledger
        self.concurrency = max(1, concurrency)
        self.pack_size = max(1, pack_size)
        self.temperature = temperature

    def run(self, state: str, jobs: list[JudgeJob]) -> JudgeResult:
        result = JudgeResult()
        for job in jobs:
            if not job.item_ids:
                continue
            self._run_job(state, job, result)
        return result

    # ------------------------------------------------------------ private

    def _run_job(self, state: str, job: JudgeJob, result: JudgeResult) -> None:
        if getattr(self.backend, "packs_items", False):
            self._run_packed(state, job, result)
        else:
            self._run_fanout(state, job, result)

    def _run_packed(self, state: str, job: JudgeJob, result: JudgeResult) -> None:
        """Jev shape: ~pack_size items ride in one request with the state."""
        for start in range(0, len(job.item_ids), self.pack_size):
            ids = job.item_ids[start : start + self.pack_size]
            texts = job.texts[start : start + self.pack_size]
            flags = job.truncated_flags[start : start + self.pack_size]
            try:
                batch = self.backend.system_one_batch(  # type: ignore[attr-defined]
                    state, texts, job.config.questions, temperature=self.temperature
                )
            except JudgeUnavailable as exc:
                self._ledger_error(job, len(ids), exc)
                for i, item_id in enumerate(ids):
                    result.errors_by_item[item_id] = str(exc)
                    result.truncated_by_item[item_id] = flags[i]
                continue
            except JudgeError as exc:
                self._ledger_error(job, len(ids), exc)
                for i, item_id in enumerate(ids):
                    result.errors_by_item[item_id] = str(exc)
                    result.truncated_by_item[item_id] = flags[i]
                continue
            self._ledger_ok(job, len(ids), getattr(self.backend, "last_call", None))
            for i, item_id in enumerate(ids):
                self._absorb(item_id, flags[i], batch[i], result)

    def _run_fanout(self, state: str, job: JudgeJob, result: JudgeResult) -> None:
        """CLM shape: one request per item; encoder amortized by the vector cache."""

        def one(idx: int) -> tuple[int, dict[str, Answer] | None, str | None]:
            try:
                return (
                    idx,
                    self.backend.system_one(  # type: ignore[attr-defined]
                        state, job.texts[idx], job.config.questions, temperature=self.temperature
                    ),
                    None,
                )
            except (JudgeError, JudgeUnavailable) as exc:
                return idx, None, str(exc)

        with ThreadPoolExecutor(max_workers=self.concurrency) as pool:
            futures = [pool.submit(one, i) for i in range(len(job.item_ids))]
            for fut in as_completed(futures):
                idx, answers, error = fut.result()
                item_id = job.item_ids[idx]
                result.truncated_by_item[item_id] = job.truncated_flags[idx]
                if error is not None:
                    result.errors_by_item[item_id] = error
                    self._ledger_error(job, 1, JudgeError(error))
                else:
                    result.answers_by_item[item_id] = answers or {}
                    self._ledger_ok(job, 1, getattr(self.backend, "last_call", None))

    def _absorb(self, item_id: str, truncated: bool, answers: dict[str, Answer], result: JudgeResult) -> None:
        result.truncated_by_item[item_id] = truncated
        if answers is None:
            result.errors_by_item[item_id] = "backend returned no answer for item"
        else:
            result.answers_by_item[item_id] = answers

    def _ledger_ok(self, job: JudgeJob, item_count: int, call: object) -> None:
        self.ledger(
            backend=self.backend.name,  # type: ignore[attr-defined]
            model=self.backend.model,  # type: ignore[attr-defined]
            request_hash=request_hash_of(job, item_count),
            item_count=item_count,
            latency_ms=getattr(call, "latency_ms", None),
            input_tokens=getattr(call, "input_tokens", None),
            output_tokens=getattr(call, "output_tokens", None),
            cost_usd=getattr(self.backend, "cost_usd", lambda _: 0.0)(getattr(call, "input_tokens", None)),
            error=None,
        )

    def _ledger_error(self, job: JudgeJob, item_count: int, exc: Exception) -> None:
        self.ledger(
            backend=self.backend.name,  # type: ignore[attr-defined]
            model=self.backend.model,  # type: ignore[attr-defined]
            request_hash=request_hash_of(job, item_count),
            item_count=item_count,
            error=str(exc)[:500],
        )


def request_hash_of(job: JudgeJob, item_count: int) -> str:
    """Stable request hash for the ledger (state not included: hashed upstream)."""
    raw = json.dumps(
        {
            "source": job.source,
            "judgeconfig": job.config.version,
            "item_count": item_count,
            "texts_sha": hashlib.sha256("\x1e".join(job.texts).encode("utf-8")).hexdigest()[:16],
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def wait_for_backend(backend: object, attempts: int = 3, delay_s: float = 5.0) -> bool:
    """Poll health(); used by the pipeline before committing to a cycle."""
    health = getattr(backend, "health", None)
    if health is None:
        return True
    for attempt in range(attempts):
        try:
            if health():
                return True
        except Exception:  # noqa: BLE001 — health probing must never raise
            pass
        if attempt < attempts - 1:
            time.sleep(delay_s)
    return False
