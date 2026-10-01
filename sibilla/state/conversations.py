"""Conversation sources for the interest-profile state (docs/03).

The state is built from the history of conversations with coding agents —
the intention carrier in the agent era (the codebase is agent *output*).
A ``ConversationSource`` protocol abstracts the agent; ``claude-jsonl`` is
the first adapter (``~/.claude/projects/<project>/<session>.jsonl``).
Hermes and Codex are future adapters behind the same seam.

Everything here is deterministic (rule 2): selection, window, budget,
redaction — code, never a model.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from sibilla.config import ConversationsConfig

# --- deterministic cleaning of user-text records -------------------------
# command/reminder wrappers carry no intent; the text inside them does where
# it is the user's own (command args are prompts the user typed)
_COMMAND_RE = re.compile(r"<command-name>.*?</command-name>\s*", re.DOTALL)
_COMMAND_MSG_RE = re.compile(r"<command-message>.*?</command-message>\s*", re.DOTALL)
_COMMAND_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_LOCAL_CMD_RE = re.compile(r"<local-command-stdout>.*?</local-command-stdout>\s*", re.DOTALL)
_SYSTEM_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>\s*", re.DOTALL)
_BASH_STDOUT_RE = re.compile(r"<bash-stdout>.*?</bash-stdout>\s*", re.DOTALL)
_BASH_STDERR_RE = re.compile(r"<bash-stderr>.*?</bash-stderr>\s*", re.DOTALL)
_BASH_INPUT_RE = re.compile(r"<bash-input>.*?</bash-input>\s*", re.DOTALL)
_INTERRUPT_RE = re.compile(r"^\[Request interrupted by user\]\s*$", re.MULTILINE)
_PASTED_RE = re.compile(r"<pasted_content[^>]*>\s*(.*?)\s*</pasted_content[^>]*>", re.DOTALL)

# --- redaction (default ON): shaped secrets never leave the machine ------
_REDACTION_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sk-[A-Za-z0-9_\-]{8,}"), "[REDACTED]"),  # OpenAI-style keys
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "[REDACTED]"),  # GitHub tokens
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "[REDACTED]"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9\-]+"), "[REDACTED]"),  # Slack
    (re.compile(r"AKIA[0-9A-Z]{16}"), "[REDACTED]"),  # AWS access key id
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{15,}"), "bearer [REDACTED]"),
    # keep the field name, redact only the value
    (re.compile(r"(?i)\b(api[_-]?key|secret|password|token)(\s*[=:]\s*)\S{8,}"), r"\1\2[REDACTED]"),
    (re.compile(r"(?i)[\w./-]*\.env\b[\w./-]*"), "[REDACTED]"),
]


def redact(text: str) -> str:
    """Strip shaped secrets; free-text business details are a documented residual risk."""
    for pattern, repl in _REDACTION_RULES:
        text = pattern.sub(repl, text)
    return text


def clean_user_text(raw: str) -> str:
    """Unwrap command/reminder/shell scaffolding, keep what the user actually typed.

    Pasted content IS intent (the user chose to paste it) — it stays, unwrapped.
    """
    args = _COMMAND_ARGS_RE.search(raw)
    text = _PASTED_RE.sub(lambda m: m.group(1), raw)  # unwrap, keep the content
    text = _COMMAND_ARGS_RE.sub("", text)
    text = _COMMAND_RE.sub("", text)
    text = _COMMAND_MSG_RE.sub("", text)
    text = _LOCAL_CMD_RE.sub("", text)
    text = _SYSTEM_REMINDER_RE.sub("", text)
    text = _BASH_STDOUT_RE.sub("", text)
    text = _BASH_STDERR_RE.sub("", text)
    text = _BASH_INPUT_RE.sub("", text)
    text = _INTERRUPT_RE.sub("", text)
    text = text.strip()
    if args and args.group(1).strip() and not text:
        text = args.group(1).strip()  # a slash command's args are the user's words
    return text


@dataclass
class Turn:
    """One user (or assistant) turn, already cleaned — the state's raw material."""

    project: str
    session: str
    title: str  # ai-title when the agent recorded one, else ""
    ts: datetime
    author: str  # "user" | "assistant"
    text: str


@dataclass
class Conversation:
    """All turns of one project inside the window (the selection unit)."""

    project: str
    turns: list[Turn] = field(default_factory=list)


class ConversationSource(Protocol):
    """The seam for agent adapters (rule 4 philosophy, applied to the state)."""

    name: str

    def conversations(self, since: datetime) -> list[Conversation]: ...


class ClaudeJsonlSource:
    """Reads ``~/.claude/projects/<project>/<session>.jsonl`` transcripts."""

    name = "claude-jsonl"

    def __init__(self, roots: list[str] | None = None):
        self.roots = [Path(r).expanduser() for r in (roots or ["~/.claude/projects"])]

    def conversations(self, since: datetime) -> list[Conversation]:
        by_project: dict[str, dict[str, list[Turn]]] = {}
        for root in self.roots:
            if not root.is_dir():
                continue
            for file in root.glob("*/*.jsonl"):
                project = self._project_name(file.parent.name)
                for turn in self._parse_session(file, project, since):
                    by_project.setdefault(project, {}).setdefault(turn.session, []).append(turn)
        return [
            Conversation(project=project, turns=self._flatten(sessions))
            for project, sessions in sorted(by_project.items())
        ]

    # ------------------------------------------------------------ parsing

    def _parse_session(self, file: Path, project: str, since: datetime):
        titles: dict[str, str] = {}
        session_id = file.stem
        turns: list[Turn] = []
        try:
            lines = file.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            rtype = rec.get("type")
            if rtype == "ai-title":
                titles[session_id] = str(rec.get("title") or "")
                continue
            if rtype not in ("user", "assistant"):
                continue
            ts = self._ts(rec.get("timestamp"))
            if ts is None or ts < since:
                continue
            text = self._text(rec)
            if not text:
                continue
            turns.append(
                Turn(
                    project=project,
                    session=session_id,
                    title=titles.get(session_id, ""),
                    ts=ts,
                    author=rtype,
                    text=text,
                )
            )
        if not turns:
            return
        # titles may arrive after the turns that reference them (streamed records)
        title = titles.get(session_id, "")
        if title:
            for t in turns:
                t.title = title
        yield from turns

    @staticmethod
    def _ts(value: Any) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    @staticmethod
    def _text(rec: dict[str, Any]) -> str:
        """Extract prose: string content, or text blocks — never tool_result/tool_use."""
        msg = rec.get("message") or {}
        content = msg.get("content")
        if isinstance(content, str):
            return clean_user_text(content) if rec.get("type") == "user" else content.strip()
        if isinstance(content, list):
            parts = [b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(p for p in parts if p).strip()
            return clean_user_text(text) if rec.get("type") == "user" else text
        return ""

    @staticmethod
    def _flatten(sessions: dict[str, list[Turn]]) -> list[Turn]:
        turns = [t for ts in sessions.values() for t in ts]
        turns.sort(key=lambda t: t.ts, reverse=True)  # most recent first
        return turns

    @staticmethod
    def _project_name(dir_name: str) -> str:
        """`-home-maeste-project-sibilla` → `sibilla`; unknown shapes stay as-is.

        Worktree suffixes (`--claude-worktrees-issue-637-runtime`) are the same
        project — stripped, so echoes of one project don't multiply sections.
        """
        m = re.match(r"-home-[^-]+-(?:project-)?(.+)", dir_name)
        name = m.group(1) if m else dir_name
        return re.sub(r"--(?:claude-)?worktrees-.*$", "", name)


def build_conversation_state(
    cfg: ConversationsConfig, today: str | None = None
) -> tuple[str, list[Conversation], bool]:
    """Deterministic selection + budget (docs/03): returns (state_text, convs, radar_used).

    Selection: window, exclude list, user-only (or user+assistant), proportional
    per-project budget capped at ~30%, most-recent-first inside each project.
    """
    include_assistant = cfg.include == "user+assistant"
    since = datetime.now().astimezone() - timedelta(days=cfg.window_days)
    excluded = {e.lower() for e in cfg.exclude_projects}

    source = ClaudeJsonlSource(cfg.roots)
    convs = [c for c in source.conversations(since) if c.project.lower() not in excluded]
    convs = [
        Conversation(project=c.project, turns=[t for t in c.turns if include_assistant or t.author == "user"])
        for c in convs
    ]
    convs = [c for c in convs if c.turns]
    if cfg.redact:  # before anything leaves the machine (docs/03 privacy note)
        for conv in convs:
            for t in conv.turns:
                t.text = redact(t.text)

    today = today or datetime.now().strftime("%Y-%m-%d")
    header = f"ACTIVE WORK PROFILE (auto-generated from agent conversations, {today})\n"
    radar_note = "\nADDITIONAL INTERESTS (RADAR.md, user-provided)\n"  # appended by the caller verbatim

    if not convs:
        return header + "(no conversations in the window — check state.conversations config)\n" + radar_note, [], False

    # proportional budget with a ~30% cap per project (deterministic)
    volumes = {c.project: sum(len(t.text) for t in c.turns) for c in convs}
    total_volume = sum(volumes.values())
    cap = max(1, int(cfg.char_budget * 0.30))
    budget = {p: min(cap, max(200, int(cfg.char_budget * v / max(1, total_volume)))) for p, v in volumes.items()}

    sections = [header]
    for conv in sorted(convs, key=lambda c: -volumes[c.project]):  # busiest project first
        lines: list[str] = []
        used = 0
        last_session = None
        for turn in conv.turns:  # already most-recent-first
            if turn.session != last_session:
                if turn.title:
                    lines.append(f"### {turn.title}")
                    used += len(turn.title) + 5
                last_session = turn.session
            line = "- " + " ".join(turn.text.split())[:400]
            if used + len(line) > budget[conv.project]:
                break
            lines.append(line)
            used += len(line) + 1
            if used >= budget[conv.project]:
                break
        if lines:
            sections.append(f"\n## {conv.project}\n" + "\n".join(lines))

    return "\n".join(sections).strip() + "\n" + radar_note, convs, True
