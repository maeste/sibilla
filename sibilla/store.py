"""SQLite store — the single local DB (docs/01 design rule 4).

Everything lands here first: items, verdicts (cache), state versions, dedup
clusters, the judge cost ledger, user labels (tune loop) and fitted
calibration thresholds. No item is ever judged twice under the same
``(item_id, state_hash, judgeconfig_version, backend, model)`` key.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    native_id TEXT NOT NULL,
    fetched_at INT NOT NULL,
    title TEXT,
    url TEXT,
    author TEXT,
    published INT,
    body_ref TEXT,
    meta JSON,
    UNIQUE(source, native_id)
);

CREATE TABLE IF NOT EXISTS bodies (
    item_id TEXT PRIMARY KEY REFERENCES items(id),
    body TEXT NOT NULL
);

-- docs/01 schema + `truncated` flag (packer budget cut, docs/01 failure table)
CREATE TABLE IF NOT EXISTS verdicts (
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    state_hash TEXT NOT NULL,
    judgeconfig_version TEXT NOT NULL,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    scores JSON,
    confidences JSON,
    truncated INT NOT NULL DEFAULT 0,
    created_at INT,
    UNIQUE(item_id, state_hash, judgeconfig_version, backend, model)
);

CREATE TABLE IF NOT EXISTS state_versions (
    hash TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    built_at INT
);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY,
    created_at INT
);

CREATE TABLE IF NOT EXISTS cluster_items (
    cluster_id INT NOT NULL REFERENCES clusters(id),
    item_id TEXT NOT NULL REFERENCES items(id),
    UNIQUE(cluster_id, item_id)
);

-- cost ledger (docs/03 fan-out scheduling: every response persisted raw)
CREATE TABLE IF NOT EXISTS judge_calls (
    id INTEGER PRIMARY KEY,
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    request_hash TEXT,
    item_count INT NOT NULL DEFAULT 1,
    latency_ms INT,
    input_tokens INT,
    output_tokens INT,
    cost_usd REAL NOT NULL DEFAULT 0.0,
    error TEXT,
    created_at INT NOT NULL
);

-- user labels closing the tune loop (docs/03 fine-tuning section)
CREATE TABLE IF NOT EXISTS labels (
    id INTEGER PRIMARY KEY,
    item_id TEXT NOT NULL REFERENCES items(id),
    state_hash TEXT,
    label TEXT NOT NULL,
    created_at INT NOT NULL,
    UNIQUE(item_id, state_hash, label)
);

-- confidence-gating thresholds fitted per (backend, model, judgeconfig_version)
CREATE TABLE IF NOT EXISTS calibrations (
    backend TEXT NOT NULL,
    model TEXT NOT NULL,
    judgeconfig_version TEXT NOT NULL,
    thresholds JSON NOT NULL,
    n_examples INT NOT NULL DEFAULT 0,
    fitted_at INT NOT NULL,
    PRIMARY KEY (backend, model, judgeconfig_version)
);

CREATE INDEX IF NOT EXISTS idx_items_source_published ON items(source, published);
CREATE INDEX IF NOT EXISTS idx_verdicts_item ON verdicts(item_id);
"""

UNIQUE_VIOLATION = "UNIQUE constraint failed"


@dataclass
class ItemRow:
    id: str
    source: str
    native_id: str
    fetched_at: int
    title: str | None = None
    url: str | None = None
    author: str | None = None
    published: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
    body: str = ""


@dataclass(frozen=True)
class VerdictKey:
    """Cache key: same state + config + backend + model ⇒ cache hit (docs/03)."""

    state_hash: str
    judgeconfig_version: str
    backend: str
    model: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.state_hash, self.judgeconfig_version, self.backend, self.model)


@dataclass
class VerdictRow:
    item_id: str
    key: VerdictKey
    scores: dict[str, Any]
    confidences: dict[str, Any]
    truncated: bool = False
    created_at: int = 0
    id: int | None = None


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------- items

    def upsert_item(self, item: ItemRow) -> bool:
        """Insert or update by (source, native_id). Returns True if newly inserted."""
        cur = self.conn.execute(
            "SELECT id FROM items WHERE source = ? AND native_id = ?",
            (item.source, item.native_id),
        )
        row = cur.fetchone()
        if row is not None:
            item.id = row["id"]
            return False
        item.id = item.id or f"{item.source}:{item.native_id}"
        self.conn.execute(
            "INSERT INTO items (id, source, native_id, fetched_at, title, url, author, published, body_ref, meta)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                item.id,
                item.source,
                item.native_id,
                item.fetched_at,
                item.title,
                item.url,
                item.author,
                item.published,
                item.id,  # body_ref: rowid-style pointer to the bodies table
                json.dumps(item.meta or {}),
            ),
        )
        if item.body:
            self.conn.execute(
                "INSERT INTO bodies (item_id, body) VALUES (?, ?)"
                " ON CONFLICT(item_id) DO UPDATE SET body = excluded.body",
                (item.id, item.body),
            )
        self.conn.commit()
        return True

    def item(self, item_id: str) -> ItemRow | None:
        row = self.conn.execute(
            "SELECT i.*, b.body FROM items i LEFT JOIN bodies b ON b.item_id = i.id WHERE i.id = ?",
            (item_id,),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def items_between(self, start: int, end: int, sources: Sequence[str] | None = None) -> list[ItemRow]:
        """Items with published ts in [start, end]; used by map build (pure function of DB)."""
        q = "SELECT i.*, b.body FROM items i LEFT JOIN bodies b ON b.item_id = i.id WHERE i.published BETWEEN ? AND ?"
        args: list[Any] = [start, end]
        if sources:
            q += f" AND i.source IN ({','.join('?' * len(sources))})"
            args.extend(sources)
        rows = self.conn.execute(q + " ORDER BY i.published DESC", args).fetchall()
        return [self._row_to_item(r) for r in rows]

    def items_without_verdict(self, source: str, key: VerdictKey) -> list[ItemRow]:
        q = (
            "SELECT i.*, b.body FROM items i"
            " LEFT JOIN bodies b ON b.item_id = i.id"
            " WHERE i.source = ? AND NOT EXISTS ("
            "   SELECT 1 FROM verdicts v WHERE v.item_id = i.id AND v.state_hash = ?"
            "   AND v.judgeconfig_version = ? AND v.backend = ? AND v.model = ?)"
        )
        rows = self.conn.execute(q, (source, *key.as_tuple())).fetchall()
        return [self._row_to_item(r) for r in rows]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> ItemRow:
        return ItemRow(
            id=row["id"],
            source=row["source"],
            native_id=row["native_id"],
            fetched_at=row["fetched_at"],
            title=row["title"],
            url=row["url"],
            author=row["author"],
            published=row["published"],
            meta=json.loads(row["meta"] or "{}"),
            body=row["body"] or "",
        )

    # ---------------------------------------------------------- verdicts

    def save_verdict(self, v: VerdictRow) -> bool:
        """Insert a verdict; returns False when the cache key already holds one."""
        try:
            self.conn.execute(
                "INSERT INTO verdicts (item_id, state_hash, judgeconfig_version, backend, model,"
                " scores, confidences, truncated, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    v.item_id,
                    v.key.state_hash,
                    v.key.judgeconfig_version,
                    v.key.backend,
                    v.key.model,
                    json.dumps(v.scores),
                    json.dumps(v.confidences),
                    int(v.truncated),
                    v.created_at or int(time.time()),
                ),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError as exc:
            if UNIQUE_VIOLATION in str(exc):
                return False
            raise

    def verdicts_for_items(self, item_ids: Sequence[str], key: VerdictKey) -> dict[str, VerdictRow]:
        if not item_ids:
            return {}
        q = (
            f"SELECT * FROM verdicts WHERE item_id IN ({','.join('?' * len(item_ids))})"
            " AND state_hash = ? AND judgeconfig_version = ? AND backend = ? AND model = ?"
        )
        rows = self.conn.execute(q, (*item_ids, *key.as_tuple())).fetchall()
        out: dict[str, VerdictRow] = {}
        for r in rows:
            out[r["item_id"]] = VerdictRow(
                item_id=r["item_id"],
                key=key,
                scores=json.loads(r["scores"] or "{}"),
                confidences=json.loads(r["confidences"] or "{}"),
                truncated=bool(r["truncated"]),
                created_at=r["created_at"] or 0,
                id=r["id"],
            )
        return out

    def latest_verdict_any_key(self, item_id: str) -> VerdictRow | None:
        row = self.conn.execute(
            "SELECT * FROM verdicts WHERE item_id = ? ORDER BY created_at DESC LIMIT 1", (item_id,)
        ).fetchone()
        if row is None:
            return None
        key = VerdictKey(row["state_hash"], row["judgeconfig_version"], row["backend"], row["model"])
        return VerdictRow(
            item_id=item_id,
            key=key,
            scores=json.loads(row["scores"] or "{}"),
            confidences=json.loads(row["confidences"] or "{}"),
            truncated=bool(row["truncated"]),
            created_at=row["created_at"] or 0,
            id=row["id"],
        )

    # ------------------------------------------------------------- state

    def save_state(self, state_hash: str, payload: str, built_at: int | None = None) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO state_versions (hash, payload, built_at) VALUES (?, ?, ?)",
            (state_hash, payload, built_at or int(time.time())),
        )
        self.conn.commit()

    def latest_state(self) -> tuple[str, str, int] | None:
        # rowid tiebreak: several states can share a built_at second
        row = self.conn.execute(
            "SELECT hash, payload, built_at FROM state_versions ORDER BY built_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return (row["hash"], row["payload"], row["built_at"]) if row else None

    def state_history(self) -> list[tuple[str, int]]:
        rows = self.conn.execute("SELECT hash, built_at FROM state_versions ORDER BY built_at").fetchall()
        return [(r["hash"], r["built_at"]) for r in rows]

    # ------------------------------------------------------- judge calls

    def save_judge_call(
        self,
        backend: str,
        model: str,
        request_hash: str | None,
        item_count: int,
        latency_ms: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        cost_usd: float = 0.0,
        error: str | None = None,
        created_at: int | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO judge_calls (backend, model, request_hash, item_count, latency_ms,"
            " input_tokens, output_tokens, cost_usd, error, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                backend,
                model,
                request_hash,
                item_count,
                latency_ms,
                input_tokens,
                output_tokens,
                cost_usd,
                error,
                created_at or int(time.time()),
            ),
        )
        self.conn.commit()

    def spend_between(self, start: int, end: int) -> float:
        row = self.conn.execute(
            "SELECT COALESCE(SUM(cost_usd), 0.0) AS s FROM judge_calls WHERE created_at BETWEEN ? AND ? AND error IS NULL",
            (start, end),
        ).fetchone()
        return float(row["s"])

    def call_errors_between(self, start: int, end: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM judge_calls WHERE created_at BETWEEN ? AND ? AND error IS NOT NULL",
            (start, end),
        ).fetchone()
        return int(row["n"])

    # ---------------------------------------------------------- clusters

    def replace_clusters(self, member_lists: Sequence[Sequence[str]], created_at: int | None = None) -> list[int]:
        """Replace the dedup state with the given clusters (each a list of item ids)."""
        ts = created_at or int(time.time())
        self.conn.execute("DELETE FROM cluster_items")
        self.conn.execute("DELETE FROM clusters")
        ids: list[int] = []
        for members in member_lists:
            if len(members) < 2:
                continue  # singletons are not clusters
            cur = self.conn.execute("INSERT INTO clusters (created_at) VALUES (?)", (ts,))
            cid = int(cur.lastrowid)
            ids.append(cid)
            self.conn.executemany(
                "INSERT OR IGNORE INTO cluster_items (cluster_id, item_id) VALUES (?, ?)",
                [(cid, m) for m in members],
            )
        self.conn.commit()
        return ids

    def cluster_of(self, item_id: str) -> tuple[int, list[str]] | None:
        row = self.conn.execute("SELECT cluster_id FROM cluster_items WHERE item_id = ?", (item_id,)).fetchone()
        if row is None:
            return None
        cid = int(row["cluster_id"])
        members = [
            r["item_id"]
            for r in self.conn.execute("SELECT item_id FROM cluster_items WHERE cluster_id = ?", (cid,)).fetchall()
        ]
        return (cid, members)

    def cluster_counts(self, start: int, end: int) -> int:
        """Number of clusters containing at least one item published in the window."""
        row = self.conn.execute(
            "SELECT COUNT(DISTINCT ci.cluster_id) AS n FROM cluster_items ci"
            " JOIN items i ON i.id = ci.item_id WHERE i.published BETWEEN ? AND ?",
            (start, end),
        ).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------ labels

    def add_label(
        self, item_id: str, label: str, state_hash: str | None = None, created_at: int | None = None
    ) -> bool:
        try:
            self.conn.execute(
                "INSERT INTO labels (item_id, state_hash, label, created_at) VALUES (?, ?, ?, ?)",
                (item_id, state_hash, label, created_at or int(time.time())),
            )
            self.conn.commit()
            return True
        except sqlite3.IntegrityError as exc:
            if UNIQUE_VIOLATION in str(exc):
                return False
            raise

    def labels(self, source: str | None = None, since: int | None = None) -> list[dict[str, Any]]:
        q = "SELECT l.*, i.source FROM labels l JOIN items i ON i.id = l.item_id WHERE 1=1"
        args: list[Any] = []
        if source:
            q += " AND i.source = ?"
            args.append(source)
        if since is not None:
            q += " AND l.created_at >= ?"
            args.append(since)
        return [dict(r) for r in self.conn.execute(q + " ORDER BY l.created_at", args).fetchall()]

    # ------------------------------------------------------ calibrations

    def save_calibration(
        self, backend: str, model: str, judgeconfig_version: str, thresholds: dict[str, float], n_examples: int
    ) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO calibrations (backend, model, judgeconfig_version, thresholds, n_examples, fitted_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (backend, model, judgeconfig_version, json.dumps(thresholds), n_examples, int(time.time())),
        )
        self.conn.commit()

    def calibration(self, backend: str, model: str, judgeconfig_version: str) -> dict[str, float] | None:
        row = self.conn.execute(
            "SELECT thresholds FROM calibrations WHERE backend = ? AND model = ? AND judgeconfig_version = ?",
            (backend, model, judgeconfig_version),
        ).fetchone()
        return json.loads(row["thresholds"]) if row else None
