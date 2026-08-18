#!/usr/bin/env python3
"""Fetch daily component closes needed to value the cached 159518 PCFs.

Only the standalone analysis directory is written. TWS is connected readonly.
"""

from __future__ import annotations

import argparse
import csv
import sys
import time as time_module
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT.parent / "赎回收益计算器"
NEW_YORK = ZoneInfo("America/New_York")
PCF_DAYS = (
    date(2026, 6, 22),
    date(2026, 6, 23),
    date(2026, 6, 25),
    date(2026, 6, 29),
    date(2026, 6, 30),
)


def _bar_day(value: object) -> date:
    if isinstance(value, datetime):
        return value.astimezone(NEW_YORK).date() if value.tzinfo else value.date()
    return value  # ib_insync daily bars normally use date


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, default=0, help="0-based symbol batch offset")
    parser.add_argument("--count", type=int, default=10, help="symbols to request in this run")
    parser.add_argument("--client-id", type=int, default=1874)
    args = parser.parse_args()
    sys.path.insert(0, str(SOURCE_ROOT))
    import szse_pcf

    try:
        from ib_insync import IB, Stock
    except ImportError as exc:
        raise SystemExit("请使用 conda ag 环境运行：缺少 ib_insync") from exc

    store = szse_pcf.SzsePcfStore(SOURCE_ROOT / "szse_pcf_cache")
    symbols = set()
    for pcf_day in PCF_DAYS:
        detail = store.ensure_fund_detail(pcf_day, "159518")
        symbols.update(
            component["UnderlyingSecurityID"].strip()
            for component in detail.components
            if component.get("UnderlyingSecurityIDSource") == "9999"
            and Decimal(component.get("ComponentShare", "0")) > 0
        )
    symbols = sorted(symbols)
    destination = ROOT / "data" / "pcf_component_daily_closes.csv"
    destination.parent.mkdir(parents=True, exist_ok=True)
    fields = ("symbol", "trade_day", "close", "bar_vwap", "volume", "con_id", "source")
    existing: dict[tuple[str, str], dict[str, str]] = {}
    if destination.exists():
        with destination.open("r", encoding="utf-8-sig", newline="") as handle:
            existing = {(row["symbol"], row["trade_day"]): row for row in csv.DictReader(handle)}
    batch = symbols[max(0, args.start): max(0, args.start) + max(1, args.count)]
    if not batch:
        raise SystemExit("请求批次为空")

    ib = IB()
    try:
        ib.connect("127.0.0.1", 7496, clientId=args.client_id, readonly=True, timeout=10)
        rows: list[dict[str, object]] = []
        for number, symbol in enumerate(batch, start=max(0, args.start) + 1):
            contract = Stock(symbol, "SMART", "USD")
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                print(f"{number}/{len(symbols)} {symbol}: contract not qualified", flush=True)
                continue
            bars = ib.reqHistoricalData(
                contract,
                endDateTime=datetime.combine(date(2026, 6, 30), time(23, 59), NEW_YORK),
                durationStr="20 D",
                barSizeSetting="1 day",
                whatToShow="TRADES",
                useRTH=True,
                formatDate=2,
            )
            by_day = {_bar_day(bar.date): bar for bar in bars}
            found = 0
            for pcf_day in PCF_DAYS:
                bar = by_day.get(pcf_day)
                if bar is None:
                    continue
                found += 1
                rows.append(
                    {
                        "symbol": symbol,
                        "trade_day": pcf_day.isoformat(),
                        "close": bar.close,
                        "bar_vwap": bar.average,
                        "volume": bar.volume,
                        "con_id": contract.conId,
                        "source": "TWS historical TRADES / 1 day / RTH",
                    }
                )
            print(f"{number}/{len(symbols)} {symbol}: {found}/{len(PCF_DAYS)} target closes", flush=True)
            time_module.sleep(2.1)
        for row in rows:
            existing[(str(row["symbol"]), str(row["trade_day"]))] = {key: str(value) for key, value in row.items()}
        with destination.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(sorted(existing.values(), key=lambda row: (row["symbol"], row["trade_day"])))
        print(f"Added {len(rows)} rows; file now has {len(existing)} rows: {destination}")
        return 0
    finally:
        if ib.isConnected():
            ib.disconnect()


if __name__ == "__main__":
    raise SystemExit(main())
