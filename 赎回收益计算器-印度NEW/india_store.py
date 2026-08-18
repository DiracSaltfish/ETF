from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

from india_models import RedemptionEvent


SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS redemption_events (
    event_id TEXT PRIMARY KEY,
    account TEXT NOT NULL,
    redeem_day TEXT NOT NULL,
    qty INTEGER NOT NULL,
    source TEXT NOT NULL,
    contract_no TEXT NOT NULL DEFAULT '',
    gross_amount TEXT,
    fee_amount TEXT,
    net_amount TEXT,
    nav_per_share TEXT,
    statement_day TEXT,
    raw_reference TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS order_plans (
    order_ref TEXT PRIMARY KEY,
    trade_day TEXT NOT NULL,
    symbol TEXT NOT NULL,
    action TEXT NOT NULL,
    quantity INTEGER NOT NULL,
    trigger_dt TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS order_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_ref TEXT NOT NULL DEFAULT '',
    event_type TEXT NOT NULL,
    order_id INTEGER,
    perm_id INTEGER,
    status TEXT NOT NULL DEFAULT '',
    filled REAL,
    remaining REAL,
    message TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_order_events_ref ON order_events(order_ref, event_id);
"""


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


class IndiaStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def upsert_redemption(self, event: RedemptionEvent) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO redemption_events (
                    event_id, account, redeem_day, qty, source, contract_no,
                    gross_amount, fee_amount, net_amount, nav_per_share,
                    statement_day, raw_reference
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    account=excluded.account,
                    redeem_day=excluded.redeem_day,
                    qty=excluded.qty,
                    source=excluded.source,
                    contract_no=excluded.contract_no,
                    gross_amount=excluded.gross_amount,
                    fee_amount=excluded.fee_amount,
                    net_amount=excluded.net_amount,
                    nav_per_share=excluded.nav_per_share,
                    statement_day=excluded.statement_day,
                    raw_reference=excluded.raw_reference
                """,
                (
                    event.event_id,
                    event.account,
                    event.redeem_day.isoformat(),
                    event.qty,
                    event.source,
                    event.contract_no,
                    _decimal_text(event.gross_amount),
                    _decimal_text(event.fee_amount),
                    _decimal_text(event.net_amount),
                    _decimal_text(event.nav_per_share),
                    event.statement_day.isoformat() if event.statement_day else None,
                    event.raw_reference,
                ),
            )

    def list_redemptions(self) -> list[RedemptionEvent]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM redemption_events ORDER BY redeem_day, account, event_id").fetchall()
        result: list[RedemptionEvent] = []
        for row in rows:
            result.append(
                RedemptionEvent(
                    event_id=row["event_id"],
                    account=row["account"],
                    redeem_day=date.fromisoformat(row["redeem_day"]),
                    qty=int(row["qty"]),
                    source=row["source"],
                    contract_no=row["contract_no"],
                    gross_amount=Decimal(row["gross_amount"]) if row["gross_amount"] else None,
                    fee_amount=Decimal(row["fee_amount"]) if row["fee_amount"] else None,
                    net_amount=Decimal(row["net_amount"]) if row["net_amount"] else None,
                    nav_per_share=Decimal(row["nav_per_share"]) if row["nav_per_share"] else None,
                    statement_day=date.fromisoformat(row["statement_day"]) if row["statement_day"] else None,
                    raw_reference=row["raw_reference"],
                )
            )
        return result

    def delete_redemption(self, event_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute("DELETE FROM redemption_events WHERE event_id=?", (event_id,))
        return cursor.rowcount > 0

    def save_order_specs(self, specs: list[object]) -> None:
        with self.connect() as connection:
            for spec in specs:
                payload = dict(vars(spec))
                payload["trade_day"] = spec.trade_day.isoformat()
                payload["trigger_dt"] = spec.trigger_dt.isoformat()
                connection.execute(
                    """
                    INSERT OR REPLACE INTO order_plans
                    (order_ref, trade_day, symbol, action, quantity, trigger_dt, payload_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        spec.order_ref,
                        spec.trade_day.isoformat(),
                        spec.symbol,
                        spec.action,
                        spec.quantity,
                        spec.trigger_dt.isoformat(),
                        json.dumps(payload, ensure_ascii=False),
                    ),
                )

    def append_order_event(self, payload: dict[str, object]) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO order_events (
                    order_ref, event_type, order_id, perm_id, status,
                    filled, remaining, message, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(payload.get("order_ref") or ""),
                    str(payload.get("event") or "unknown"),
                    int(payload["order_id"]) if payload.get("order_id") is not None else None,
                    int(payload["perm_id"]) if payload.get("perm_id") is not None else None,
                    str(payload.get("status") or ""),
                    float(payload["filled"]) if payload.get("filled") is not None else None,
                    float(payload["remaining"]) if payload.get("remaining") is not None else None,
                    str(payload.get("message") or ""),
                    json.dumps(payload, ensure_ascii=False, default=str),
                ),
            )

    def list_order_events(self, order_ref: str = "") -> list[dict[str, object]]:
        with self.connect() as connection:
            if order_ref:
                rows = connection.execute(
                    "SELECT * FROM order_events WHERE order_ref=? ORDER BY event_id", (order_ref,)
                ).fetchall()
            else:
                rows = connection.execute("SELECT * FROM order_events ORDER BY event_id").fetchall()
        return [dict(row) for row in rows]

    def sent_order_refs(self) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT DISTINCT order_ref
                FROM order_events
                WHERE order_ref <> ''
                  AND event_type IN (
                      'submitted', 'openOrder', 'orderStatus',
                      'execDetails', 'commissionReport', 'sync'
                  )
                """
            ).fetchall()
        return {str(row["order_ref"]) for row in rows}

    def next_order_ref(self, base_ref: str) -> str:
        """Reuse an unsent plan, or create Rn only after the prior order was cancelled."""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT order_ref FROM order_plans WHERE order_ref=? OR order_ref LIKE ?",
                (base_ref, f"{base_ref}_R%"),
            ).fetchall()
            known = {
                str(row["order_ref"])
                for row in rows
                if str(row["order_ref"]) == base_ref
                or str(row["order_ref"]).startswith(base_ref + "_R")
            }
            if not known:
                return base_ref

            def revision(value: str) -> int:
                if value == base_ref:
                    return 1
                suffix = value.removeprefix(base_ref + "_R")
                return int(suffix) if suffix.isdigit() else 1

            latest_ref = max(known, key=revision)
            event = connection.execute(
                """
                SELECT event_type, status FROM order_events
                WHERE order_ref=? ORDER BY event_id DESC LIMIT 1
                """,
                (latest_ref,),
            ).fetchone()
        if event is None:
            return latest_ref
        status = str(event["status"] or "").strip().upper()
        event_type = str(event["event_type"] or "").strip().lower()
        terminal_cancel = status in {"CANCELLED", "APICANCELLED", "INACTIVE"} or event_type in {
            "cancelled",
            "apicancelled",
        }
        if not terminal_cancel:
            return latest_ref
        return f"{base_ref}_R{revision(latest_ref) + 1}"
