"""RADAR.md — the per-user manual override (docs/03 state construction step 3).

Appended verbatim to the generated state: the escape hatch for interests
invisible from code. No structure is imposed; an empty or placeholder file
contributes nothing.
"""

from __future__ import annotations

import re
from pathlib import Path

_PLACEHOLDER_RE = re.compile(r"^\s*#\s*RADAR\.md\s*$", re.IGNORECASE)


def load_radar_md(path: str | Path) -> str:
    """Return the RADAR.md text verbatim, or '' when missing/empty/placeholder."""
    p = Path(path)
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8").strip()
    if not text or _PLACEHOLDER_RE.match(text):
        return ""
    return text
