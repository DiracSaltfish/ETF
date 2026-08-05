from __future__ import annotations

import argparse
import asyncio
import time as time_module
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from market_data import XopDailyPrice, upsert_xop_prices


ROOT = Path(__file__).resolve().parent
NEW_YORK = ZoneInfo("America/New_York")


def _vwap(bars: list[object], start: time, end: time) -> Decimal | None:
    selected = [bar for bar in bars if start <= bar.date.astimezone(NEW_YORK).time() < end]
    volume = sum((Decimal(str(bar.volume)) for bar in selected), Decimal("0"))
    if volume > 0:
        return sum(
            (Decimal(str(bar.average)) * Decimal(str(bar.volume)) for bar in selected), Decimal("0")
        ) / volume
    if selected:
        return sum((Decimal(str(bar.close)) for bar in selected), Decimal("0")) / Decimal(len(selected))
    return None


def _minute_close(bars: list[object], minute: time) -> Decimal | None:
    """Return the close of the one-minute bar labelled with ``minute`` in New York."""
    matches = [
        bar for bar in bars
        if isinstance(bar.date, datetime) and bar.date.astimezone(NEW_YORK).time().replace(second=0, microsecond=0) == minute
    ]
    if not matches:
        return None
    return Decimal(str(matches[-1].close))


def _ensure_asyncio_event_loop() -> tuple[asyncio.AbstractEventLoop, bool]:
    """Provide ib_insync an event loop when called from a Qt worker thread."""
    try:
        return asyncio.get_event_loop(), False
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop, True


def fetch_prices(
    start_day: date,
    end_day: date,
    *,
    host: str,
    port: int,
    client_id: int,
    intraday_days: set[date],
) -> list[XopDailyPrice]:
    try:
        from ib_insync import IB, Stock
    except ImportError as exc:
        raise RuntimeError("TWS 回填需要 ib_insync；运行环境请使用 conda ag") from exc

    loop, owns_loop = _ensure_asyncio_event_loop()
    ib = IB()
    try:
        ib.connect(host, port, clientId=client_id, readonly=True, timeout=10)
        contract = Stock("XOP", "SMART", "USD")
        if not ib.qualifyContracts(contract):
            raise RuntimeError("TWS 无法确认 XOP 合约")
        duration_days = max(2, (end_day - start_day).days + 7)
        daily_bars = ib.reqHistoricalData(
            contract,
            endDateTime=datetime.combine(end_day, time(23, 59), NEW_YORK),
            durationStr=f"{duration_days} D",
            barSizeSetting="1 day",
            whatToShow="TRADES",
            useRTH=True,
            formatDate=2,
        )
        daily_close = {
            (bar.date.date() if isinstance(bar.date, datetime) else bar.date): Decimal(str(bar.close))
            for bar in daily_bars
            if start_day <= (bar.date.date() if isinstance(bar.date, datetime) else bar.date) <= end_day
        }
        result: list[XopDailyPrice] = []
        for trade_day in sorted(daily_close):
            minute_bars: list[object] = []
            if trade_day in intraday_days:
                minute_bars = list(
                    ib.reqHistoricalData(
                        contract,
                        endDateTime=datetime.combine(trade_day, time(16, 1), NEW_YORK),
                        durationStr="1 D",
                        barSizeSetting="1 min",
                        whatToShow="TRADES",
                        useRTH=True,
                        formatDate=2,
                    )
                )
                time_module.sleep(2.1)
            last_1559 = _minute_close(minute_bars, time(15, 59))
            last_1600 = Decimal(str(minute_bars[-1].close)) if minute_bars else None
            result.append(
                XopDailyPrice(
                    symbol="XOP",
                    trade_day=trade_day,
                    close=daily_close[trade_day],
                    vwap_1540_1550=_vwap(minute_bars, time(15, 40), time(15, 50)),
                    vwap_1540_1600=_vwap(minute_bars, time(15, 40), time(16, 0)),
                    vwap_1554_1557=_vwap(minute_bars, time(15, 54), time(15, 57)),
                    last_1559=last_1559,
                    last_1600=last_1600,
                    source="tws_historical",
                )
            )
        return result
    finally:
        if ib.isConnected():
            ib.disconnect()
        if owns_loop:
            loop.close()
            asyncio.set_event_loop(None)


def main() -> int:
    parser = argparse.ArgumentParser(description="从 TWS 只读回填 XOP 日线和收盘窗口价格")
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--intraday-days", default="", help="逗号分隔的 YYYY-MM-DD")
    parser.add_argument("--csv", type=Path, default=ROOT / "market_data" / "xop_prices.csv")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=8888)
    args = parser.parse_args()
    intraday_days = {
        date.fromisoformat(item.strip()) for item in args.intraday_days.split(",") if item.strip()
    }
    prices = fetch_prices(
        args.start,
        args.end,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        intraday_days=intraday_days,
    )
    upsert_xop_prices(args.csv, prices)
    print(f"已写入 {len(prices)} 个 XOP 交易日：{args.csv.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
