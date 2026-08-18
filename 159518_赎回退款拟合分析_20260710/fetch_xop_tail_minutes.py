#!/usr/bin/env python3
"""Read-only TWS download of XOP one-minute bars for the final 30 minutes.

All outputs stay under this analysis directory.  The redemption calculator is
never imported or written by this script.
"""

from __future__ import annotations

import csv
import time as time_module
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_DAYS = (
    date(2026, 6, 22),
    date(2026, 6, 23),
    date(2026, 6, 25),
    date(2026, 6, 29),
    date(2026, 6, 30),
)


def main() -> int:
    try:
        from ib_insync import IB, Stock
    except ImportError as exc:
        raise SystemExit("请使用 conda ag 环境运行：缺少 ib_insync") from exc

    destination = ROOT / "data" / "xop_tail_1m.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    ib = IB()
    try:
        ib.connect("127.0.0.1", 7496, clientId=1871, readonly=True, timeout=10)
        contract = Stock("XOP", "SMART", "USD", primaryExchange="ARCA")
        if not ib.qualifyContracts(contract):
            raise RuntimeError("TWS 无法确认 XOP 合约")
        rows: list[dict[str, object]] = []
        for trade_day in DEFAULT_DAYS:
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=datetime.combine(trade_day, time(16, 1), NEW_YORK),
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
            )
            selected = [
                bar for bar in bars
                if bar.date.astimezone(NEW_YORK).date() == trade_day
                and time(15, 30) <= bar.date.astimezone(NEW_YORK).time() < time(16, 0)
            ]
            print(f"{trade_day}: received {len(selected)} tail minutes")
            for bar in selected:
                ny_dt = bar.date.astimezone(NEW_YORK)
                rows.append(
                    {
                        "trade_day": trade_day.isoformat(),
                        "minute_end_ny": ny_dt.isoformat(),
                        "minute": ny_dt.strftime("%H:%M"),
                        "open": bar.open,
                        "high": bar.high,
                        "low": bar.low,
                        "close": bar.close,
                        "bar_vwap": bar.average,
                        "volume": bar.volume,
                        "bar_count": bar.barCount,
                        "source": "TWS historical TRADES / 1 min / RTH",
                    }
                )
            time_module.sleep(2.1)
        with destination.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else ["trade_day"])
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} rows to {destination}")
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
