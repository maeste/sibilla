"""Delivery — Telegram file send (docs/04 daily flow).

Cron → `sibilla map --deliver telegram` → home channel message with the
HTML attached. Unconfigured delivery degrades to manual (open the HTML),
never fails the pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path

import requests

from sibilla.config import DeliveryConfig

TELEGRAM_API = "https://api.telegram.org"


class DeliveryResult:
    def __init__(self, sent: bool, detail: str):
        self.sent = sent
        self.detail = detail

    def __str__(self) -> str:
        return ("delivered: " if self.sent else "not delivered: ") + self.detail


def deliver_telegram(cfg: DeliveryConfig, html_path: str | Path, summary: str) -> DeliveryResult:
    """Send the map HTML as a document with the one-line summary as caption."""
    token = os.environ.get(cfg.token_env, "")
    if not token:
        return DeliveryResult(False, f"{cfg.token_env} not set — open the HTML manually")
    if not cfg.chat_id:
        return DeliveryResult(False, "delivery.chat_id not configured — open the HTML manually")
    path = Path(html_path)
    try:
        with path.open("rb") as fh:
            resp = requests.post(
                f"{TELEGRAM_API}/bot{token}/sendDocument",
                data={"chat_id": cfg.chat_id, "caption": summary[:1000]},
                files={"document": (path.name, fh, "text/html")},
                timeout=60,
            )
    except requests.RequestException as exc:
        return DeliveryResult(False, f"telegram unreachable: {exc}")
    if resp.status_code != 200:
        return DeliveryResult(False, f"telegram {resp.status_code}: {resp.text[:200]}")
    return DeliveryResult(True, f"sent to chat {cfg.chat_id}")
