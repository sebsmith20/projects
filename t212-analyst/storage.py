"""SQLite persistence layer for snapshots, briefings, and trade proposals."""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class Storage:
    """Thread-safe / process-safe SQLite store with atomic proposal locking."""

    def __init__(self, db_path: Path) -> None:
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        try:
            yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS portfolio_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    env TEXT NOT NULL,
                    free_cash REAL NOT NULL,
                    total_cash REAL,
                    invested REAL,
                    ppl REAL,
                    positions_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS market_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    t212_ticker TEXT,
                    price REAL,
                    prev_close REAL,
                    change_pct REAL,
                    volume REAL,
                    news_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS briefings (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    trigger_reason TEXT NOT NULL,
                    summary_markdown TEXT NOT NULL,
                    raw_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS trade_proposals (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    briefing_id INTEGER REFERENCES briefings(id),
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    t212_ticker TEXT NOT NULL,
                    yahoo_symbol TEXT,
                    action TEXT NOT NULL CHECK(action IN ('BUY', 'SELL', 'HOLD')),
                    quantity REAL NOT NULL,
                    order_type TEXT NOT NULL CHECK(order_type IN ('MARKET', 'LIMIT')),
                    limit_price REAL,
                    reasoning TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING'
                        CHECK(status IN ('PENDING', 'APPROVED_IN_FLIGHT', 'EXECUTED', 'FAILED', 'REJECTED', 'EXPIRED')),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    t212_order_id TEXT,
                    execution_response_json TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_proposals_status
                    ON trade_proposals(status);
                CREATE INDEX IF NOT EXISTS idx_briefings_ts
                    ON briefings(timestamp);
                """
            )

    def save_portfolio_snapshot(
        self,
        env: str,
        cash_data: dict[str, Any],
        positions: list[dict[str, Any]],
    ) -> int:
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO portfolio_snapshots
                    (timestamp, env, free_cash, total_cash, invested, ppl, positions_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    utc_now_iso(),
                    env,
                    float(cash_data.get("free", 0.0) or 0.0),
                    cash_data.get("total"),
                    cash_data.get("invested"),
                    cash_data.get("ppl"),
                    json.dumps(positions),
                ),
            )
            return int(cur.lastrowid)

    def save_market_snapshots(self, quotes: list[dict[str, Any]]) -> None:
        ts = utc_now_iso()
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO market_snapshots
                    (timestamp, symbol, t212_ticker, price, prev_close, change_pct, volume, news_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        ts,
                        q["yahoo_symbol"],
                        q.get("t212_ticker"),
                        q.get("price"),
                        q.get("prev_close"),
                        q.get("change_pct"),
                        q.get("volume"),
                        json.dumps(q.get("news", [])),
                    )
                    for q in quotes
                ],
            )

    def get_last_briefing_date(self) -> str | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT timestamp FROM briefings ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if not row:
                return None
            return str(row["timestamp"])[:10]

    def save_briefing_and_proposals(
        self,
        trigger_reason: str,
        summary_markdown: str,
        raw_payload: dict[str, Any],
        ideas: list[dict[str, Any]],
    ) -> tuple[int, list[int]]:
        now = utc_now_iso()
        proposal_ids: list[int] = []
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    INSERT INTO briefings (timestamp, trigger_reason, summary_markdown, raw_json)
                    VALUES (?, ?, ?, ?)
                    """,
                    (now, trigger_reason, summary_markdown, json.dumps(raw_payload)),
                )
                briefing_id = int(cur.lastrowid)

                for idea in ideas:
                    action = str(idea.get("action", "HOLD")).upper()
                    qty = float(idea.get("quantity", 0) or 0)
                    # Only create actionable proposals for BUY/SELL with positive quantity
                    if action not in {"BUY", "SELL"} or qty <= 0:
                        continue
                    order_type = str(idea.get("order_type", "MARKET")).upper()
                    if order_type not in {"MARKET", "LIMIT"}:
                        order_type = "MARKET"
                    limit_price = idea.get("limit_price")
                    if order_type == "MARKET":
                        limit_price = None

                    p_cur = conn.execute(
                        """
                        INSERT INTO trade_proposals (
                            briefing_id, created_at, updated_at, t212_ticker, yahoo_symbol,
                            action, quantity, order_type, limit_price, reasoning,
                            status, idempotency_key
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?)
                        """,
                        (
                            briefing_id,
                            now,
                            now,
                            idea["t212_ticker"],
                            idea.get("yahoo_symbol"),
                            action,
                            abs(qty),
                            order_type,
                            float(limit_price) if limit_price is not None else None,
                            idea.get("reasoning", ""),
                            str(uuid.uuid4()),
                        ),
                    )
                    proposal_ids.append(int(p_cur.lastrowid))
                conn.execute("COMMIT")
            except Exception:
                conn.execute("ROLLBACK")
                raise
        return briefing_id, proposal_ids

    def list_proposals(self, status: str | None = None, limit: int = 25) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM trade_proposals WHERE status = ? ORDER BY id DESC LIMIT ?",
                    (status.upper(), limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM trade_proposals ORDER BY id DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [dict(r) for r in rows]

    def get_proposal(self, proposal_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM trade_proposals WHERE id = ?", (proposal_id,)
            ).fetchone()
            return dict(row) if row else None

    def claim_proposal_for_execution(self, proposal_id: int) -> dict[str, Any] | None:
        """Atomically transition PENDING -> APPROVED_IN_FLIGHT.

        Returns the proposal dict if this caller won the lock, or None if already
        claimed, executed, rejected, or missing.
        """
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                cur = conn.execute(
                    """
                    UPDATE trade_proposals
                    SET status = 'APPROVED_IN_FLIGHT', updated_at = ?
                    WHERE id = ? AND status = 'PENDING'
                    """,
                    (now, proposal_id),
                )
                if cur.rowcount != 1:
                    conn.execute("ROLLBACK")
                    return None
                row = conn.execute(
                    "SELECT * FROM trade_proposals WHERE id = ?", (proposal_id,)
                ).fetchone()
                conn.execute("COMMIT")
                return dict(row) if row else None
            except Exception:
                conn.execute("ROLLBACK")
                raise

    def mark_proposal_result(
        self,
        proposal_id: int,
        status: str,
        t212_order_id: str | None = None,
        response_payload: dict[str, Any] | None = None,
    ) -> None:
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE trade_proposals
                SET status = ?, updated_at = ?, t212_order_id = ?, execution_response_json = ?
                WHERE id = ?
                """,
                (
                    status,
                    now,
                    t212_order_id,
                    json.dumps(response_payload) if response_payload is not None else None,
                    proposal_id,
                ),
            )

    def reject_proposal(self, proposal_id: int) -> bool:
        now = utc_now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE trade_proposals
                SET status = 'REJECTED', updated_at = ?
                WHERE id = ? AND status = 'PENDING'
                """,
                (now, proposal_id),
            )
            return cur.rowcount == 1
