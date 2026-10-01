"""Deterministic state builder: agent conversations → profile text + state_hash (docs/03).

The interest profile comes from the history of conversations with coding
agents (see ``conversations.py``) — assembled in code; the model never
invents the profile. ``RADAR.md`` is appended verbatim. ``sha256(state_text)``
keys every verdict.

The GitHub-derived builder was removed by design: in the agent era the
codebase is agent output; the intention lives in the conversations
(docs/00 pillar 1, rewritten by the conversation-state pivot).
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from datetime import datetime

from sibilla.config import StateConfig
from sibilla.state.conversations import Conversation, build_conversation_state
from sibilla.state.radar_md import load_radar_md


class StateBuilderError(RuntimeError):
    """State construction failed — caller falls back to last good state."""


@dataclass
class State:
    text: str
    state_hash: str
    conversations: list[Conversation] = field(default_factory=list)
    radar_md_used: bool = False
    built_at: int = field(default_factory=lambda: int(time.time()))


def build_state(cfg: StateConfig, today: str | None = None) -> State:
    """Assemble the state text exactly in the docs/03 shape and hash it."""
    today = today or datetime.now().strftime("%Y-%m-%d")
    try:
        text, convs, _has_turns = build_conversation_state(cfg.conversations, today=today)
    except OSError as exc:  # unreadable roots — degrade loudly, keep the fallback path
        raise StateBuilderError(f"conversation sources unreadable: {exc}") from exc

    radar = load_radar_md(cfg.radar_md)
    if radar:
        text = text.replace(
            "\nADDITIONAL INTERESTS (RADAR.md, user-provided)\n",
            f"\nADDITIONAL INTERESTS (RADAR.md, user-provided)\n\n{radar}\n",
        )
    else:
        text = text.replace("\nADDITIONAL INTERESTS (RADAR.md, user-provided)\n", "")

    text = text.strip() + "\n"
    return State(
        text=text,
        state_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        conversations=convs,
        radar_md_used=bool(radar),
    )
