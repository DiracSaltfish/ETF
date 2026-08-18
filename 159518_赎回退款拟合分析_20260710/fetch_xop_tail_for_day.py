#!/usr/bin/env python3
"""Read-only TWS fetch of one XOP tail session into the analysis directory."""

from __future__ import annotations

import argparse
import csv
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
NEW_YORK = ZoneInfo("America/New_York")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--day", required=True, type=date.fromisoformat)
    parser.add_argument("--client-id", type=int, default=1880)
    args = parser.parse_args()
    try:
        from ib_insync import IB, Stock
    except ImportError as exc:
        raise SystemExit("请使用 conda ag 环境运行：缺少 ib_insync") from exc

    destination = ROOT / "data" / f"xop_tail_1m_{args.day:%Y%m%d}.csv"
    ib = IB()
    try:
        ib.connect("127.0.0.1", 7496, clientId=args.client_id, readonly=True, timeout=10)
        contract = Stock("XOP", "SMART", "USD", primaryExchange="ARCA")
        if not ib.qualifyContracts(contract):
            raise RuntimeError("TWS 无法确认 XOP 合约")
        bars = ib.reqHistoricalData(
            contract,
            endDateTime=datetime.combine(args.day, time(16, 1), NEW_YORK),
            durationStr="1 D",
            barSizeSetting="1 min",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
        rows = []
        for bar in bars:
            ny_dt = bar.date.astimezone(NEW_YORK)
            if ny_dt.date() != args.day or not (time(15, 30) <= ny_dt.time() < time(16, 0)):
                continue
            rows.append(
                {
                    "trade_day": args.day.isoformat(),
                    "minute_start_ny": ny_dt.isoformat(),
                    "minute": ny_dt.strftime("%H:%M"),
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "bar_vwap": bar.average,
                    "volume": bar.volume,
                    "source": "TWS historical TRADES / 1 min / RTH",
                }
            )
        daily = ib.reqHistoricalData(
            contract,
            endDateTime=datetime.combine(args.day, time(23, 59), NEW_YORK),
            durationStr="2 D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
        daily_close = next(
            (
                bar.close for bar in daily
                if (bar.date.astimezone(NEW_YORK).date() if isinstance(bar.date, datetime) and bar.date.tzinfo else bar.date) == args.day
            ),
            None,
        )
        if len(rows) != 30:
            raise RuntimeError(f"预期 30 根尾盘分钟 bar，实际 {len(rows)} 根")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {len(rows)} bars: {destination}")
        print(f"Official daily close proxy: {daily_close}")
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
