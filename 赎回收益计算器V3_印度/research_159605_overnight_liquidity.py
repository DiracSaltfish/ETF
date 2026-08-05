#!/usr/bin/env python3
"""Read-only IBKR Overnight liquidity study for SZ159605's seven ADRs.

The study uses the official daily PCF share counts and IBKR's OVERNIGHT
historical BID, ASK, TRADES, and BID_ASK tick data. It has no order path and
connects to TWS with ``readonly=True``.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import statistics
import time as time_module
import xml.etree.ElementTree as ET
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
CACHE_ROOT = ROOT / "szse_pcf_cache"
OUTPUT_ROOT = ROOT / "临时研究数据"
US_TZ = ZoneInfo("America/New_York")
SH_TZ = ZoneInfo("Asia/Shanghai")
UTC = ZoneInfo("UTC")
ADR_CODES = ("BZ", "PDD", "QFIN", "TAL", "TME", "VIPS", "YMM")
MIN_PACING_SECONDS = 2.1
EXPECTED_OVERNIGHT_MINUTES = 470
EXPECTED_CHINA_OVERLAP_MINUTES = 240


@dataclass(frozen=True)
class DailyPcf:
    session_date: date
    shares: dict[str, float]
    source_path: Path


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def direct_text(node: ET.Element, name: str) -> str:
    for child in node:
        if local_name(child.tag) == name:
            return str(child.text or "").strip()
    return ""


def load_pcf(path: Path, session_date: date) -> DailyPcf:
    root = ET.parse(path).getroot()
    security_id = next(
        (str(node.text or "").strip() for node in root if local_name(node.tag) == "SecurityID"),
        "",
    )
    if security_id != "159605":
        raise ValueError(f"{path} is not an SZ159605 PCF")
    shares: dict[str, float] = {}
    for node in root.iter():
        if local_name(node.tag) != "Component":
            continue
        code = direct_text(node, "UnderlyingSecurityID").upper()
        if code not in ADR_CODES:
            continue
        quantity = float(direct_text(node, "ComponentShare") or 0)
        if quantity <= 0:
            raise ValueError(f"{path}: invalid {code} ComponentShare={quantity}")
        shares[code] = quantity
    if set(shares) != set(ADR_CODES):
        raise ValueError(f"{path}: ADR set mismatch: {sorted(shares)}")
    return DailyPcf(session_date=session_date, shares=shares, source_path=path)


def available_pcf_days(cache_root: Path, end_date: date, sessions: int) -> list[DailyPcf]:
    candidates: list[tuple[date, Path]] = []
    for path in cache_root.glob("*/xml/159605.xml"):
        try:
            day = date.fromisoformat(path.parent.parent.name)
        except ValueError:
            continue
        if day <= end_date:
            candidates.append((day, path))
    selected = sorted(candidates)[-sessions:]
    if len(selected) != sessions:
        raise FileNotFoundError(f"need {sessions} PCFs through {end_date}, found {len(selected)}")
    return [load_pcf(path, day) for day, path in selected]


def ensure_loop() -> tuple[asyncio.AbstractEventLoop, bool]:
    try:
        return asyncio.get_event_loop(), False
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop, True


def as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
    text = str(value).strip()
    for pattern in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=US_TZ).astimezone(UTC)
        except ValueError:
            pass
    raise ValueError(f"cannot parse TWS timestamp: {value!r}")


def overnight_session_date(timestamp_utc: datetime) -> date | None:
    local = timestamp_utc.astimezone(US_TZ)
    if local.time() >= time(20, 0):
        return local.date() + timedelta(days=1)
    if local.time() < time(3, 50):
        return local.date()
    return None


def in_china_overlap(timestamp_utc: datetime) -> bool:
    local = timestamp_utc.astimezone(US_TZ)
    # July daylight-time mapping for the two live A-share sessions:
    # 09:30–11:30 BJT -> 21:30–23:30 ET, 13:00–15:00 BJT -> 01:00–03:00 ET.
    # Deliberately exclude the 90-minute China lunch break.
    return time(21, 30) <= local.time() < time(23, 30) or time(1, 0) <= local.time() < time(3, 0)


def percentile(values: list[float], quantile: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    position = (len(clean) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return clean[lower]
    return clean[lower] + (clean[upper] - clean[lower]) * (position - lower)


def fmt(value: float | None, digits: int = 2) -> str:
    return "—" if value is None or not math.isfinite(value) else f"{value:,.{digits}f}"


class Fetcher:
    def __init__(self, host: str, port: int, client_id: int, pace_seconds: float) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.pace_seconds = max(pace_seconds, MIN_PACING_SECONDS)
        self.last_request_at: float | None = None
        self.ib = None
        self.loop = None
        self.owns_loop = False
        self.contracts: dict[str, object] = {}

    def __enter__(self) -> "Fetcher":
        from ib_insync import IB

        self.loop, self.owns_loop = ensure_loop()
        self.ib = IB()
        self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=True, timeout=10)
        return self

    def __exit__(self, *_: object) -> None:
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()
        if self.owns_loop and self.loop is not None:
            self.loop.close()
            asyncio.set_event_loop(None)

    def pace(self) -> None:
        if self.last_request_at is not None:
            remaining = self.pace_seconds - (time_module.monotonic() - self.last_request_at)
            if remaining > 0:
                time_module.sleep(remaining)
        self.last_request_at = time_module.monotonic()

    def contract(self, symbol: str):
        if symbol in self.contracts:
            return self.contracts[symbol]
        from ib_insync import Stock

        assert self.ib is not None
        qualified = self.ib.qualifyContracts(Stock(symbol, "OVERNIGHT", "USD"))
        if len(qualified) != 1:
            raise RuntimeError(f"could not uniquely qualify OVERNIGHT {symbol}")
        self.contracts[symbol] = qualified[0]
        return qualified[0]

    def bars(self, symbol: str, end_date: date, sessions: int, what: str) -> list[object]:
        assert self.ib is not None
        self.pace()
        end = datetime.combine(end_date, time(3, 50), US_TZ)
        return list(
            self.ib.reqHistoricalData(
                self.contract(symbol),
                endDateTime=end,
                durationStr=f"{sessions} D",
                barSizeSetting="1 min",
                whatToShow=what,
                useRTH=False,
                formatDate=2,
                timeout=60,
            )
        )

    def size_ticks(self, symbol: str, session_date: date) -> list[object]:
        assert self.ib is not None
        self.pace()
        # 03:00 ET is 15:00 Beijing in US daylight time. The returned last
        # 1,000 quote updates form a bounded late-China-session size sample.
        end = datetime.combine(session_date, time(3, 0), US_TZ)
        return list(
            self.ib.reqHistoricalTicks(
                self.contract(symbol),
                startDateTime="",
                endDateTime=end,
                numberOfTicks=1000,
                whatToShow="BID_ASK",
                useRth=False,
                ignoreSize=False,
            )
        )


def bar_records(symbol: str, what: str, bars: Iterable[object], valid_days: set[date]) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for bar in bars:
        timestamp_utc = as_datetime(getattr(bar, "date"))
        session_date = overnight_session_date(timestamp_utc)
        if session_date not in valid_days:
            continue
        records.append(
            {
                "symbol": symbol,
                "series": what,
                "session_date": session_date.isoformat(),
                "timestamp_et": timestamp_utc.astimezone(US_TZ).isoformat(),
                "timestamp_beijing": timestamp_utc.astimezone(SH_TZ).isoformat(),
                "timestamp_utc": timestamp_utc.isoformat(),
                "open": float(getattr(bar, "open", math.nan)),
                "high": float(getattr(bar, "high", math.nan)),
                "low": float(getattr(bar, "low", math.nan)),
                "close": float(getattr(bar, "close", math.nan)),
                "volume": float(getattr(bar, "volume", math.nan)),
                "bar_count": int(getattr(bar, "barCount", -1)),
                "china_overlap": in_china_overlap(timestamp_utc),
            }
        )
    return records


def tick_records(symbol: str, ticks: Iterable[object], session_date: date) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for tick in ticks:
        timestamp_utc = as_datetime(getattr(tick, "time"))
        if overnight_session_date(timestamp_utc) != session_date:
            continue
        bid = float(getattr(tick, "priceBid", math.nan))
        ask = float(getattr(tick, "priceAsk", math.nan))
        bid_size = float(getattr(tick, "sizeBid", math.nan))
        ask_size = float(getattr(tick, "sizeAsk", math.nan))
        records.append(
            {
                "symbol": symbol,
                "session_date": session_date.isoformat(),
                "timestamp_et": timestamp_utc.astimezone(US_TZ).isoformat(),
                "timestamp_beijing": timestamp_utc.astimezone(SH_TZ).isoformat(),
                "timestamp_utc": timestamp_utc.isoformat(),
                "bid": bid,
                "ask": ask,
                "bid_size": bid_size,
                "ask_size": ask_size,
            }
        )
    return records


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def daily_metrics(
    pcf_by_day: dict[date, DailyPcf],
    bars_by_key: dict[tuple[str, str], list[dict[str, object]]],
    ticks_by_key: dict[tuple[str, date], list[dict[str, object]]],
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    daily: list[dict[str, object]] = []
    joined_minutes: list[dict[str, object]] = []
    for day, pcf in sorted(pcf_by_day.items()):
        for symbol in ADR_CODES:
            bids = {
                row["timestamp_utc"]: row
                for row in bars_by_key.get((symbol, "BID"), [])
                if row["session_date"] == day.isoformat()
            }
            asks = {
                row["timestamp_utc"]: row
                for row in bars_by_key.get((symbol, "ASK"), [])
                if row["session_date"] == day.isoformat()
            }
            trades = [
                row for row in bars_by_key.get((symbol, "TRADES"), [])
                if row["session_date"] == day.isoformat()
            ]
            spreads: list[float] = []
            china_spreads: list[float] = []
            for timestamp in sorted(set(bids) & set(asks)):
                bid = float(bids[timestamp]["close"])
                ask = float(asks[timestamp]["close"])
                if not (bid > 0 and ask >= bid):
                    continue
                midpoint = (bid + ask) / 2
                spread = ask - bid
                spread_bps = spread / midpoint * 10000
                row = {
                    "symbol": symbol,
                    "session_date": day.isoformat(),
                    "timestamp_et": bids[timestamp]["timestamp_et"],
                    "timestamp_beijing": bids[timestamp]["timestamp_beijing"],
                    "timestamp_utc": timestamp,
                    "bid": bid,
                    "ask": ask,
                    "midpoint": midpoint,
                    "spread": spread,
                    "spread_bps": spread_bps,
                    "china_overlap": bool(bids[timestamp]["china_overlap"]),
                }
                joined_minutes.append(row)
                spreads.append(spread_bps)
                if row["china_overlap"]:
                    china_spreads.append(spread_bps)

            size_rows = ticks_by_key.get((symbol, day), [])
            valid_sizes = [
                row for row in size_rows
                if float(row["bid"]) > 0
                and float(row["ask"]) >= float(row["bid"])
                and float(row["bid_size"]) >= 0
                and float(row["ask_size"]) >= 0
            ]
            quantity = pcf.shares[symbol]
            bid_sizes = [float(row["bid_size"]) for row in valid_sizes]
            ask_sizes = [float(row["ask_size"]) for row in valid_sizes]
            size_span_minutes = None
            if len(valid_sizes) >= 2:
                first = datetime.fromisoformat(str(valid_sizes[0]["timestamp_utc"]))
                last = datetime.fromisoformat(str(valid_sizes[-1]["timestamp_utc"]))
                size_span_minutes = (last - first).total_seconds() / 60
            positive_trade_rows = [
                row for row in trades
                if math.isfinite(float(row["volume"])) and float(row["volume"]) > 0
            ]
            trade_volume = sum(float(row["volume"]) for row in positive_trade_rows)
            daily.append(
                {
                    "session_date": day.isoformat(),
                    "symbol": symbol,
                    "pcf_shares": quantity,
                    "quote_minutes": len(spreads),
                    "overnight_quote_coverage_pct": len(spreads) / EXPECTED_OVERNIGHT_MINUTES * 100,
                    "china_quote_minutes": len(china_spreads),
                    "china_quote_coverage_pct": len(china_spreads) / EXPECTED_CHINA_OVERLAP_MINUTES * 100,
                    "median_spread_bps": percentile(china_spreads, 0.50),
                    "p90_spread_bps": percentile(china_spreads, 0.90),
                    "p95_spread_bps": percentile(china_spreads, 0.95),
                    "max_spread_bps": percentile(china_spreads, 1.00),
                    "trade_minutes": len(positive_trade_rows),
                    "overnight_trade_volume": trade_volume,
                    "trade_volume_to_pcf_qty": trade_volume / quantity if quantity > 0 else None,
                    "size_tick_count": len(valid_sizes),
                    "size_sample_span_minutes": size_span_minutes,
                    "median_bid_size": percentile(bid_sizes, 0.50),
                    "p10_bid_size": percentile(bid_sizes, 0.10),
                    "median_ask_size": percentile(ask_sizes, 0.50),
                    "pct_bid_size_ge_pcf_qty": (
                        sum(size >= quantity for size in bid_sizes) / len(bid_sizes) * 100 if bid_sizes else None
                    ),
                    "pct_ask_size_ge_pcf_qty": (
                        sum(size >= quantity for size in ask_sizes) / len(ask_sizes) * 100 if ask_sizes else None
                    ),
                }
            )
    return daily, joined_minutes


def aggregate_metrics(daily: list[dict[str, object]]) -> list[dict[str, object]]:
    grouped: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in daily:
        grouped[str(row["symbol"])].append(row)
    output: list[dict[str, object]] = []
    for symbol in ADR_CODES:
        rows = grouped[symbol]
        spread_medians = [float(row["median_spread_bps"]) for row in rows if row["median_spread_bps"] is not None]
        spread_p95s = [float(row["p95_spread_bps"]) for row in rows if row["p95_spread_bps"] is not None]
        coverages = [float(row["china_quote_coverage_pct"]) for row in rows]
        volume_ratios = [float(row["trade_volume_to_pcf_qty"]) for row in rows if row["trade_volume_to_pcf_qty"] is not None]
        size_rates = [float(row["pct_bid_size_ge_pcf_qty"]) for row in rows if row["pct_bid_size_ge_pcf_qty"] is not None]
        median_spread = statistics.median(spread_medians) if spread_medians else None
        worst_p95 = max(spread_p95s) if spread_p95s else None
        min_coverage = min(coverages) if coverages else 0.0
        min_volume_ratio = min(volume_ratios) if volume_ratios else 0.0
        min_size_rate = min(size_rates) if size_rates else None

        if (
            median_spread is not None
            and median_spread <= 20
            and worst_p95 is not None
            and worst_p95 <= 60
            and min_coverage >= 95
            and min_volume_ratio >= 10
            and min_size_rate is not None
            and min_size_rate >= 90
        ):
            grade = "A-报价层面较充足"
        elif (
            median_spread is not None
            and median_spread <= 50
            and worst_p95 is not None
            and worst_p95 <= 150
            and min_coverage >= 90
            and min_volume_ratio >= 3
            and min_size_rate is not None
            and min_size_rate >= 70
        ):
            grade = "B-可研究但需限价拆单"
        else:
            grade = "C-不宜假设可直接成交"
        output.append(
            {
                "symbol": symbol,
                "sessions": len(rows),
                "pcf_shares_latest": rows[-1]["pcf_shares"] if rows else None,
                "median_of_daily_median_spread_bps": median_spread,
                "worst_daily_p95_spread_bps": worst_p95,
                "minimum_china_quote_coverage_pct": min_coverage,
                "minimum_trade_volume_to_pcf_qty": min_volume_ratio,
                "minimum_bid_size_sufficiency_pct": min_size_rate,
                "quote_liquidity_grade": grade,
            }
        )
    return output


def basket_metrics(
    daily: list[dict[str, object]], joined: list[dict[str, object]]
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    quote_groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in joined:
        if bool(row["china_overlap"]):
            quote_groups[(str(row["session_date"]), str(row["symbol"]))].append(row)

    components: list[dict[str, object]] = []
    by_day: dict[str, list[dict[str, object]]] = defaultdict(list)
    for row in daily:
        session_date = str(row["session_date"])
        symbol = str(row["symbol"])
        quotes = quote_groups[(session_date, symbol)]
        midpoints = [float(quote["midpoint"]) for quote in quotes]
        spreads = [float(quote["spread"]) for quote in quotes]
        median_midpoint = percentile(midpoints, 0.50)
        median_spread = percentile(spreads, 0.50)
        quantity = float(row["pcf_shares"])
        notional = quantity * median_midpoint if median_midpoint is not None else None
        half_spread_cost = quantity * median_spread / 2 if median_spread is not None else None
        component = {
            "session_date": session_date,
            "symbol": symbol,
            "pcf_shares": quantity,
            "median_midpoint": median_midpoint,
            "median_spread": median_spread,
            "component_notional_usd": notional,
            "midpoint_to_bid_cost_usd": half_spread_cost,
        }
        components.append(component)
        by_day[session_date].append(component)

    baskets: list[dict[str, object]] = []
    for session_date, rows in sorted(by_day.items()):
        notionals = [float(row["component_notional_usd"]) for row in rows if row["component_notional_usd"] is not None]
        costs = [float(row["midpoint_to_bid_cost_usd"]) for row in rows if row["midpoint_to_bid_cost_usd"] is not None]
        basket_notional = sum(notionals)
        basket_cost = sum(costs)
        baskets.append(
            {
                "session_date": session_date,
                "adr_basket_notional_usd": basket_notional,
                "midpoint_to_bid_cost_usd": basket_cost,
                "weighted_midpoint_to_bid_cost_bps": (
                    basket_cost / basket_notional * 10000 if basket_notional > 0 else None
                ),
            }
        )
    return baskets, components


def build_report(
    daily: list[dict[str, object]],
    aggregate: list[dict[str, object]],
    baskets: list[dict[str, object]],
    dates: list[date],
) -> str:
    lines = [
        "# 159605 ADR Overnight 流动性研究",
        "",
        f"样本：`{dates[0]}` 至 `{dates[-1]}` 的 {len(dates)} 个 Overnight session；",
        "美东窗口 `20:00–03:50`，主要决策窗口为对应中国盘中的 `21:30–23:30 ET` 与 `01:00–03:00 ET`（排除午休）。",
        "数据来自 IBKR `OVERNIGHT` 历史 BID、ASK、TRADES 与历史 BID_ASK ticks；全程只读，无下单路径。",
        "",
        "> 评级只评价历史报价与成交可见度，不代表当日可借券。实际做空仍须逐日检查 shortable shares、借券费、订单路由和真实成交回报。",
        "",
        "## 汇总结论",
        "",
        "| ADR | 最新PCF股数 | 日中位价差中位数(bp) | 最差单日P95(bp) | 最低中国窗口覆盖率 | 最低成交量/PCF股数 | 最低Bid数量满足率 | 评级 |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |",
    ]
    for row in aggregate:
        lines.append(
            f"| {row['symbol']} | {fmt(row['pcf_shares_latest'], 0)} | "
            f"{fmt(row['median_of_daily_median_spread_bps'])} | {fmt(row['worst_daily_p95_spread_bps'])} | "
            f"{fmt(row['minimum_china_quote_coverage_pct'])}% | {fmt(row['minimum_trade_volume_to_pcf_qty'])}x | "
            f"{fmt(row['minimum_bid_size_sufficiency_pct'])}% | {row['quote_liquidity_grade']} |"
        )
    lines.extend(
        [
            "",
            "## 精确ADR篮子穿价成本",
            "",
            "按每日PCF股数和中国交易时段的中位Bid/Ask计算；若市价卖出到Bid，理论成本为半个spread。",
            "这是非同步的历史中位数估算，不包含冲击成本、借券费与拒单风险。",
            "",
            "| 日期 | 7只ADR篮子名义市值(USD) | 中间价到Bid成本(USD) | 加权成本(bp) |",
            "| --- | ---: | ---: | ---: |",
        ]
    )
    for row in baskets:
        lines.append(
            f"| {row['session_date']} | {fmt(row['adr_basket_notional_usd'], 0)} | "
            f"{fmt(row['midpoint_to_bid_cost_usd'])} | {fmt(row['weighted_midpoint_to_bid_cost_bps'])} |"
        )
    lines.extend(
        [
            "",
            "## 评级口径",
            "",
            "- A：日中位价差≤20bp、最差日P95≤60bp、报价覆盖≥95%、成交量至少为PCF数量10倍、Bid数量满足率≥90%。",
            "- B：日中位价差≤50bp、最差日P95≤150bp、报价覆盖≥90%、成交量至少为PCF数量3倍、Bid数量满足率≥70%。",
            "- C：未满足上述条件；不能把历史报价直接视为可成交。",
            "- Bid/Ask一分钟线可能包含交易所延续报价；历史tick数量样本取每个session在中国收盘前的最后1,000次报价更新，样本覆盖时长已保存在daily_summary.csv。",
            "",
            "## 每日明细",
            "",
            "| 日期 | ADR | PCF股数 | 中位spread(bp) | P95(bp) | 中国窗口覆盖 | 夜盘成交量 | Bid数量满足率 |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in daily:
        lines.append(
            f"| {row['session_date']} | {row['symbol']} | {fmt(row['pcf_shares'], 0)} | "
            f"{fmt(row['median_spread_bps'])} | {fmt(row['p95_spread_bps'])} | "
            f"{fmt(row['china_quote_coverage_pct'])}% | {fmt(row['overnight_trade_volume'], 0)} | "
            f"{fmt(row['pct_bid_size_ge_pcf_qty'])}% |"
        )
    lines.extend(
        [
            "",
            "## 使用限制",
            "",
            "1. 历史可见Bid数量不等于你排在队列首位，也不保证短卖订单获准。",
            "2. TRADES成交量只能证明市场发生过成交，不能证明你的PCF数量可在同一价格一次成交。",
            "3. Overnight成交和报价的交易日期按session结束日归档；北京时间与美东时间均保存在原始CSV中。",
            "4. 若实际策略在其他时点建仓，应从raw_minute_quotes.csv和raw_size_ticks.csv重新切片，不能直接套用本报告评级。",
        ]
    )
    return "\n".join(lines) + "\n"


def run(args: argparse.Namespace) -> Path:
    cache_root = args.cache_root.expanduser().resolve()
    pcfs = available_pcf_days(cache_root, args.end_date, args.sessions)
    pcf_by_day = {item.session_date: item for item in pcfs}
    valid_days = set(pcf_by_day)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = (
        args.output_dir.expanduser().resolve()
        if args.output_dir
        else OUTPUT_ROOT / f"159605_overnight_liquidity_{pcfs[0].session_date:%Y%m%d}_{pcfs[-1].session_date:%Y%m%d}_{stamp}"
    )
    output_dir.mkdir(parents=True, exist_ok=False)

    bars_by_key: dict[tuple[str, str], list[dict[str, object]]] = {}
    ticks_by_key: dict[tuple[str, date], list[dict[str, object]]] = {}
    errors: list[dict[str, str]] = []
    with Fetcher(args.host, args.port, args.client_id, args.pace_seconds) as fetcher:
        total = len(ADR_CODES) * 3 + len(ADR_CODES) * len(pcfs)
        completed = 0
        for symbol in ADR_CODES:
            for what in ("BID", "ASK", "TRADES"):
                completed += 1
                try:
                    records = bar_records(symbol, what, fetcher.bars(symbol, pcfs[-1].session_date, len(pcfs), what), valid_days)
                    bars_by_key[(symbol, what)] = records
                    if not records:
                        errors.append({"symbol": symbol, "stage": what, "error": "NO_DATA returned by IBKR"})
                    print(f"[{completed}/{total}] {symbol} {what}: {len(records)} rows", flush=True)
                except Exception as exc:
                    errors.append({"symbol": symbol, "stage": what, "error": str(exc)})
                    print(f"[{completed}/{total}] {symbol} {what} failed: {exc}", flush=True)
            for pcf in pcfs:
                completed += 1
                try:
                    records = tick_records(symbol, fetcher.size_ticks(symbol, pcf.session_date), pcf.session_date)
                    ticks_by_key[(symbol, pcf.session_date)] = records
                    print(f"[{completed}/{total}] {symbol} {pcf.session_date} size ticks: {len(records)}", flush=True)
                except Exception as exc:
                    errors.append({"symbol": symbol, "stage": f"size_ticks_{pcf.session_date}", "error": str(exc)})
                    print(f"[{completed}/{total}] {symbol} {pcf.session_date} ticks failed: {exc}", flush=True)

    daily, joined = daily_metrics(pcf_by_day, bars_by_key, ticks_by_key)
    aggregate = aggregate_metrics(daily)
    baskets, basket_components = basket_metrics(daily, joined)
    raw_bars = [row for rows in bars_by_key.values() for row in rows]
    raw_ticks = [row for rows in ticks_by_key.values() for row in rows]
    write_csv(output_dir / "raw_bars.csv", raw_bars, [
        "symbol", "series", "session_date", "timestamp_et", "timestamp_beijing", "timestamp_utc",
        "open", "high", "low", "close", "volume", "bar_count", "china_overlap",
    ])
    write_csv(output_dir / "raw_minute_quotes.csv", joined, [
        "symbol", "session_date", "timestamp_et", "timestamp_beijing", "timestamp_utc",
        "bid", "ask", "midpoint", "spread", "spread_bps", "china_overlap",
    ])
    write_csv(output_dir / "raw_size_ticks.csv", raw_ticks, [
        "symbol", "session_date", "timestamp_et", "timestamp_beijing", "timestamp_utc",
        "bid", "ask", "bid_size", "ask_size",
    ])
    daily_fields = list(daily[0]) if daily else []
    aggregate_fields = list(aggregate[0]) if aggregate else []
    write_csv(output_dir / "daily_summary.csv", daily, daily_fields)
    write_csv(output_dir / "aggregate_summary.csv", aggregate, aggregate_fields)
    write_csv(output_dir / "basket_summary.csv", baskets, list(baskets[0]) if baskets else [])
    write_csv(
        output_dir / "basket_components.csv",
        basket_components,
        list(basket_components[0]) if basket_components else [],
    )
    write_csv(output_dir / "errors.csv", errors, ["symbol", "stage", "error"])
    (output_dir / "report.md").write_text(
        build_report(daily, aggregate, baskets, [item.session_date for item in pcfs]), encoding="utf-8"
    )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "mode": "read_only_159605_overnight_liquidity",
        "sessions": [item.session_date.isoformat() for item in pcfs],
        "pcf_sources": [str(item.source_path) for item in pcfs],
        "tws": {
            "host": args.host,
            "port": args.port,
            "client_id": args.client_id,
            "readonly": True,
            "exchange": "OVERNIGHT",
            "pace_seconds": args.pace_seconds,
        },
        "errors": errors,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only SZ159605 ADR Overnight liquidity study")
    parser.add_argument("--end-date", type=date.fromisoformat, default=date.today())
    parser.add_argument("--sessions", type=int, default=5)
    parser.add_argument("--cache-root", type=Path, default=CACHE_ROOT)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=90620)
    parser.add_argument("--pace-seconds", type=float, default=MIN_PACING_SECONDS)
    args = parser.parse_args()
    if args.sessions < 2 or args.sessions > 10:
        parser.error("--sessions must be between 2 and 10")
    output = run(args)
    print(f"output: {output}")
    print(f"report: {output / 'report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
