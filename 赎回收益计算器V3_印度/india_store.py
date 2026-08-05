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
