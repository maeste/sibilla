"""TypeSafe Jev backend — the hosted alternative (docs/03).

Same primitives, hosted, pay-per-token ($0.042/Mtok input, output free).
Packs many items per 64k request; state rides along once per request.
Requires ``api_key_env`` (default ``TYPESAFE_API_KEY``).
"""

from __future__ import annotations

from sibilla.judge.base import HttpJudgeBackend


class TypesafeBackend(HttpJudgeBackend):
    name = "typesafe"
    packs_items = True  # Jev shape: ~170 items per packed request
    default_base_url = "https://api.typesafe.ai"
    default_api_key_env = "TYPESAFE_API_KEY"
    requires_api_key = True  # the SDK key is mandatory — missing key = auth problem
    input_cost_per_mtok = 0.042
