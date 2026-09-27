"""TypeSafe Jev backend — the hosted alternative (docs/03).

Same primitives, hosted, pay-per-token ($0.042/Mtok input, output free).
Packs many items per 64k request; state rides along once per request.
Requires ``api_key_env`` (default ``TYPESAFE_API_KEY``).
"""

from __future__ import annotations

from sibilla.judge.base import HttpJudgeBackend


class TypesafeBackend(HttpJudgeBackend):
    name = "typesafe"
    packs_items = False  # measured reality (v0.2 API): one content per request,
    # all questions mixed in — fan-out like CLM, bounded by judge.concurrency
    default_base_url = "https://api.typesafe.ai"
    default_api_key_env = "TYPESAFE_API_KEY"
    requires_api_key = True  # the SDK key is mandatory — missing key = auth problem
    endpoint_rank = None  # no /v1/rank in the hosted v0.2: rank is emulated via systemone
    input_cost_per_mtok = 0.042
