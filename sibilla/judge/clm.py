"""CLM backend — the local reference deployment (docs/03).

Contrastive Language Model serving a TypeSafe-compatible API on loopback.
One state embed (vector cache), one request per item; the packer fans out.
Marginal cost $0. ``CLM_API_KEY`` is optional for a loopback instance.
"""

from __future__ import annotations

from sibilla.judge.base import HttpJudgeBackend


class ClmBackend(HttpJudgeBackend):
    name = "clm"
    packs_items = False  # fan-out shape; encoder time amortized by the vector cache
    default_base_url = "http://127.0.0.1:8700"
    default_api_key_env = "CLM_API_KEY"
    input_cost_per_mtok = 0.0
