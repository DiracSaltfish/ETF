"""只读拉取 159605 PCF 成分的 TWS 行情，并生成开平仓研究报告。

本脚本的范围刻意限定为“证券现金替代腿”：不读取、不预测实际现金替代退款，也不
把输出当作可下单指令。它把原始 TWS 数据写到项目内 ``临时研究数据/``，与正式账务
数据隔离，便于人工复核与随时清理。

默认会读取最近缓存的 159605 PCF，拉取：

* 23 只港股和 7 只美国 ADR 的 T 日 RTH 1 分钟 TRADES；
* KWEB 的 T 日 RTH 1 分钟 TRADES；
* KWEB 与 7 只 ADR 在美股开盘、收盘前的历史 Bid/Ask tick 窗口。

人民币折算不使用 TWS 的离岸外汇数据，而是强制使用外汇交易中心公布的赎回 T 日
``USD/CNY``、``HKD/CNY`` 收盘参考价。

所有 TWS 连接均使用 ``readonly=True`` 和独立 client id，脚本没有下单路径。
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time as time_module
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo

import fx_rates


ROOT = Path(__file__).resolve().parent
CACHE_ROOT = ROOT / "szse_pcf_cache"
RESEARCH_TEMP_ROOT = ROOT / "临时研究数据"
HK_TZ = ZoneInfo("Asia/Hong_Kong")
US_TZ = ZoneInfo("America/New_York")
UTC = ZoneInfo("UTC")
MIN_PACING_SECONDS = 2.1
US_ADR_CODES = frozenset({"BZ", "PDD", "QFIN", "TAL", "TME", "VIPS", "YMM"})


@dataclass(frozen=True)
class PcfComponent:
    code: str
    symbol: str
    shares: Decimal
    market: str  # HK or US
    currency: str
    source: str


@dataclass(frozen=True)
class ContractSpec:
    key: str
    symbol: str
    market: str  # HK / US / FX
    currency: str
    shares: Decimal | None
    display_name: str
    pcf_code: str = ""


@dataclass(frozen=True)
class SessionSummary:
    key: str
    market: str
    currency: str
    shares: Decimal | None
    trade_date: str
    open: Decimal | None
    close: Decimal | None
    close_window_vwap: Decimal | None
    close_window_volume: Decimal | None
    bar_count: int
    source: str


@dataclass(frozen=True)
class CfetsSettlementFx:
    trade_date: date
    usd_cny: Decimal
    hkd_cny: Decimal
    usd_close_time: str
    hkd_close_time: str
    source: str = "CFETS_REFERENCE_RATE"


def _decimal(value: str | Decimal | int | float, label: str) -> Decimal:
    try:
        result = Decimal(str(value).strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"{label} 不是有效数字：{value!r}") from exc
    if result < 0:
        raise ValueError(f"{label} 不能为负数：{value!r}")
    return result


def pcf_path(trading_day: date, cache_root: Path = CACHE_ROOT) -> Path:
    return cache_root / trading_day.isoformat() / "xml" / "159605.xml"


def latest_cached_pcf_date(cache_root: Path = CACHE_ROOT) -> date:
    candidates: list[date] = []
    for path in cache_root.glob("*/xml/159605.xml"):
        try:
            candidates.append(date.fromisoformat(path.parent.parent.name))
        except ValueError:
            continue
    if not candidates:
        raise FileNotFoundError(f"未找到 159605 PCF 缓存：{cache_root}")
    return max(candidates)


def default_output_dir(pcf_day: date, root: Path = RESEARCH_TEMP_ROOT) -> Path:
    """Create no files yet; return a unique project-local research directory."""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base = root / f"159605_tws_{pcf_day:%Y%m%d}_{stamp}"
    candidate = base
    suffix = 2
    while candidate.exists():
        candidate = root / f"{base.name}_{suffix}"
        suffix += 1
    return candidate


def load_pcf_components(path: Path) -> tuple[PcfComponent, ...]:
    """Extract the actual 30 securities; drop the virtual 159900 cash row."""
    root = ET.parse(path).getroot()
    namespace = {"x": "http://ts.szse.cn/Fund"}
    security_id = (root.findtext("x:SecurityID", namespaces=namespace) or "").strip()
    if security_id != "159605":
        raise ValueError(f"{path} 不是 159605 PCF（SecurityID={security_id!r}）")

    components: list[PcfComponent] = []
    for item in root.findall(".//x:Component", namespace):
        code = (item.findtext("x:UnderlyingSecurityID", namespaces=namespace) or "").strip().upper()
        symbol = (item.findtext("x:UnderlyingSymbol", namespaces=namespace) or code).strip()
        source = (item.findtext("x:UnderlyingSecurityIDSource", namespaces=namespace) or "").strip()
        shares_text = item.findtext("x:ComponentShare", namespaces=namespace) or "0"
        if code == "159900":
            continue
        shares = _decimal(shares_text, f"{code} ComponentShare")
        if shares == 0:
            continue
        # Current PCF identifies HK with source 103 and US ADRs with source 9999.
        # Keep the code-based fallback so an ordinary PCF source-code change cannot
        # silently classify an ADR as a Hong Kong security.
        market = "US" if code in US_ADR_CODES or source == "9999" else "HK"
        components.append(
            PcfComponent(
                code=code,
                symbol=symbol,
                shares=shares,
                market=market,
                currency="USD" if market == "US" else "HKD",
                source=source,
            )
        )
    if len(components) != 30:
        raise ValueError(f"{path} 应有 30 只可交易成分，实际解析到 {len(components)} 只")
    return tuple(components)


def build_contract_specs(components: Iterable[PcfComponent]) -> tuple[ContractSpec, ...]:
    specs: list[ContractSpec] = []
    for component in components:
        # IBKR SEHK symbol uses no leading zero, e.g. 0700 -> 700.
        ib_symbol = component.code.lstrip("0") if component.market == "HK" else component.code
        specs.append(
            ContractSpec(
                key=f"{component.market}_{component.code}",
                symbol=ib_symbol or component.code,
                market=component.market,
                currency=component.currency,
                shares=component.shares,
                display_name=component.symbol,
                pcf_code=component.code,
            )
        )
    specs.append(ContractSpec("US_KWEB", "KWEB", "US", "USD", None, "KWEB"))
    return tuple(specs)


def load_cfets_settlement_fx(
    *,
    trading_day: date,
    csv_path: Path,
    refresh: bool,
) -> CfetsSettlementFx:
    """Load the two official CFETS T-day close prices required by 159605.

    There is deliberately no IB/USDCNH fallback: a missing official close makes
    the CNY valuation incomplete, so the caller must stop instead of silently
    substituting an offshore FX quote.
    """
    store = fx_rates.FxRateStore(csv_path)
    if refresh:
        store.refresh_cfets_date(trading_day)
    rows = {
        str(row.get("pair") or ""): row
        for row in store.load_day_records(trading_day)
        if row.get("source") == fx_rates.CFETS_SOURCE and row.get("quote_time") == "CLOSE"
    }
    missing = [pair for pair in fx_rates.CFETS_SETTLEMENT_PAIRS if pair not in rows]
    if missing:
        raise ValueError(
            f"缺少赎回T日外汇交易中心收盘价：{', '.join(missing)}；"
            "不能以IB离岸汇率替代"
        )
    try:
        usd_cny = Decimal(str(rows["USD/CNY"]["rate"]))
        hkd_cny = Decimal(str(rows["HKD/CNY"]["rate"]))
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("CFETS收盘价格式无效") from exc
    if usd_cny <= 0 or hkd_cny <= 0:
        raise ValueError("CFETS收盘价必须大于0")
    return CfetsSettlementFx(
        trade_date=trading_day,
        usd_cny=usd_cny,
        hkd_cny=hkd_cny,
        usd_close_time=str(rows["USD/CNY"].get("derived_from") or "CLOSE"),
        hkd_close_time=str(rows["HKD/CNY"].get("derived_from") or "CLOSE"),
    )


def _ensure_asyncio_event_loop() -> tuple[asyncio.AbstractEventLoop, bool]:
    try:
        return asyncio.get_event_loop(), False
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop, True


def _as_local_datetime(value: object, timezone: ZoneInfo) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone)
        return value.astimezone(timezone)
    text = str(value).strip()
    for pattern in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, pattern).replace(tzinfo=timezone)
        except ValueError:
            pass
    raise ValueError(f"无法解析 TWS 时间戳：{value!r}")


def _stringify_decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _write_csv(path: Path, fieldnames: tuple[str, ...], rows: Iterable[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _read_csv(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


class TwsResearchFetcher:
    """A paced, read-only TWS historical-data client with per-contract errors."""

    def __init__(self, *, host: str, port: int, client_id: int, pace_seconds: float) -> None:
        self.host = host
        self.port = port
        self.client_id = client_id
        self.pace_seconds = max(float(pace_seconds), MIN_PACING_SECONDS)
        self._last_request_at: float | None = None
        self.errors: list[dict[str, str]] = []
        self._contracts: dict[str, object] = {}
        self.ib = None
        self._owns_loop = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def __enter__(self) -> "TwsResearchFetcher":
        try:
            from ib_insync import IB
        except ImportError as exc:
            raise RuntimeError("TWS 研究脚本需要 ib_insync；请使用 conda ag 环境") from exc
        self._loop, self._owns_loop = _ensure_asyncio_event_loop()
        self.ib = IB()
        self.ib.connect(self.host, self.port, clientId=self.client_id, readonly=True, timeout=10)
        return self

    def __exit__(self, *_: object) -> None:
        if self.ib is not None and self.ib.isConnected():
            self.ib.disconnect()
        if self._owns_loop and self._loop is not None:
            self._loop.close()
            asyncio.set_event_loop(None)

    def _pace(self) -> None:
        if self._last_request_at is not None:
            remaining = self.pace_seconds - (time_module.monotonic() - self._last_request_at)
            if remaining > 0:
                time_module.sleep(remaining)
        self._last_request_at = time_module.monotonic()

    def _contract(self, spec: ContractSpec):
        cached = self._contracts.get(spec.key)
        if cached is not None:
            return cached
        assert self.ib is not None
        from ib_insync import Stock

        if spec.market == "HK":
            contract = Stock(spec.symbol, "SEHK", "HKD")
        elif spec.market == "US":
            contract = Stock(spec.symbol, "SMART", "USD")
        else:
            raise ValueError(f"未知合约类型：{spec}")
        self._pace()
        qualified = self.ib.qualifyContracts(contract)
        if not qualified:
            raise RuntimeError(f"TWS 无法确认合约 {spec.key} / {spec.symbol}")
        self._contracts[spec.key] = qualified[0]
        return qualified[0]

    def bars(self, spec: ContractSpec, trade_day: date) -> list[object]:
        assert self.ib is not None
        contract = self._contract(spec)
        timezone = HK_TZ if spec.market == "HK" else US_TZ
        what = "TRADES"
        # 16:05 is only an end anchor. useRTH removes after-hours prints.
        end = datetime.combine(trade_day, time(16, 5), timezone)
        self._pace()
        return list(
            self.ib.reqHistoricalData(
                contract,
                endDateTime=end,
                durationStr="1 D",
                barSizeSetting="1 min",
                whatToShow=what,
                useRTH=True,
                formatDate=2,
            )
        )

    def bid_ask_ticks(self, spec: ContractSpec, trade_day: date, marker: str) -> list[object]:
        """Fetch a bounded US open/close window for KWEB and the seven ADRs.

        Historical bid/ask ticks are intentionally limited to US instruments so
        that the first research pull stays below IBKR historical-data pacing
        limits. Hong Kong stocks still have full-session TRADES bars.
        """
        assert self.ib is not None
        if spec.market != "US":
            return []
        contract = self._contract(spec)
        end_time = time(9, 35) if marker == "open" else time(16, 0)
        end = datetime.combine(trade_day, end_time, US_TZ)
        self._pace()
        return list(
            self.ib.reqHistoricalTicks(
                contract,
                startDateTime="",
                endDateTime=end,
                numberOfTicks=1000,
                whatToShow="BID_ASK",
                useRth=True,
                ignoreSize=False,
            )
        )

    def record_error(self, spec: ContractSpec, stage: str, exc: Exception) -> None:
        self.errors.append({"key": spec.key, "symbol": spec.symbol, "stage": stage, "error": str(exc)})


def bar_rows(bars: Iterable[object], timezone: ZoneInfo) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for bar in bars:
        local = _as_local_datetime(getattr(bar, "date"), timezone)
        rows.append(
            {
                "timestamp_local": local.isoformat(),
                "timestamp_utc": local.astimezone(UTC).isoformat(),
                "open": str(getattr(bar, "open", "")),
                "high": str(getattr(bar, "high", "")),
                "low": str(getattr(bar, "low", "")),
                "close": str(getattr(bar, "close", "")),
                "volume": str(getattr(bar, "volume", "")),
                "average": str(getattr(bar, "average", "")),
                "bar_count": str(getattr(bar, "barCount", "")),
            }
        )
    return rows


def summarise_bars(spec: ContractSpec, trade_day: date, raw_rows: list[dict[str, object]]) -> SessionSummary:
    timezone = HK_TZ if spec.market == "HK" else US_TZ
    selected: list[tuple[datetime, Decimal, Decimal, Decimal]] = []
    for row in raw_rows:
        local = datetime.fromisoformat(str(row["timestamp_local"]))
        if local.date() != trade_day:
            continue
        try:
            close = _decimal(str(row["close"]), "bar close")
        except ValueError:
            continue
        # Some historical instruments report volume/average=-1. Treat those
        # as an unweighted close series instead of discarding the observation.
        try:
            average_raw = Decimal(str(row["average"]).strip())
        except (InvalidOperation, AttributeError):
            average_raw = Decimal("-1")
        try:
            volume_raw = Decimal(str(row["volume"]).strip())
        except (InvalidOperation, AttributeError):
            volume_raw = Decimal("-1")
        average = average_raw if average_raw > 0 else close
        volume = volume_raw if volume_raw > 0 else Decimal("0")
        selected.append((local, close, average, volume))
    selected.sort(key=lambda item: item[0])
    open_price = selected[0][1] if selected else None
    close_price = selected[-1][1] if selected else None
    close_start = time(15, 50)
    close_window = [item for item in selected if close_start <= item[0].astimezone(timezone).time() <= time(16, 0)]
    volume = sum((item[3] for item in close_window), Decimal("0"))
    if volume > 0:
        close_vwap = sum((item[2] * item[3] for item in close_window), Decimal("0")) / volume
    elif close_window:
        close_vwap = sum((item[1] for item in close_window), Decimal("0")) / Decimal(len(close_window))
    else:
        close_vwap = None
    return SessionSummary(
        key=spec.key,
        market=spec.market,
        currency=spec.currency,
        shares=spec.shares,
        trade_date=trade_day.isoformat(),
        open=open_price,
        close=close_price,
        close_window_vwap=close_vwap,
        close_window_volume=volume if close_window else None,
        bar_count=len(selected),
        source="TWS historical TRADES",
    )


def tick_rows(ticks: Iterable[object], timezone: ZoneInfo) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for tick in ticks:
        local = _as_local_datetime(getattr(tick, "time"), timezone)
        rows.append(
            {
                "timestamp_local": local.isoformat(),
                "timestamp_utc": local.astimezone(UTC).isoformat(),
                "bid_price": str(getattr(tick, "priceBid", "")),
                "ask_price": str(getattr(tick, "priceAsk", "")),
                "bid_size": str(getattr(tick, "sizeBid", "")),
                "ask_size": str(getattr(tick, "sizeAsk", "")),
                "bid_past_low": str(getattr(tick, "bidPastLow", "")),
                "ask_past_high": str(getattr(tick, "askPastHigh", "")),
            }
        )
    return rows


def _summary_map(summaries: Iterable[SessionSummary]) -> dict[str, SessionSummary]:
    return {summary.key: summary for summary in summaries}


def _value(value: Decimal | None) -> str:
    return "不可用" if value is None else f"{value:,.2f}"


def _price_at_or_before(rows: Iterable[dict[str, object]], target: datetime) -> Decimal | None:
    selected: list[tuple[datetime, Decimal]] = []
    target_utc = target.astimezone(UTC)
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(str(row["timestamp_utc"])).astimezone(UTC)
            price = _decimal(str(row["close"]), "bar close")
        except (ValueError, KeyError):
            continue
        if timestamp <= target_utc:
            selected.append((timestamp, price))
    return max(selected, key=lambda item: item[0])[1] if selected else None


def _portfolio_value_at(
    components: Iterable[PcfComponent],
    raw_bars: dict[str, list[dict[str, object]]],
    target: datetime,
    settlement_fx: CfetsSettlementFx,
) -> Decimal | None:
    total = Decimal("0")
    for component in components:
        price = _price_at_or_before(raw_bars.get(f"{component.market}_{component.code}", []), target)
        if price is None:
            return None
        rate = settlement_fx.usd_cny if component.market == "US" else settlement_fx.hkd_cny
        total += component.shares * price * rate
    return total


def _pct_change(start: Decimal | None, end: Decimal | None) -> Decimal | None:
    if start is None or end is None or start == 0:
        return None
    return (end / start - 1) * Decimal("100")


def _timing_sample(
    *,
    pcf_day: date,
    components: tuple[PcfComponent, ...],
    raw_bars: dict[str, list[dict[str, object]]],
    settlement_fx: CfetsSettlementFx,
) -> dict[str, Decimal | None]:
    """Calculate the two risk windows relevant to the proposed split hedge."""
    hk_components = tuple(item for item in components if item.market == "HK")
    us_components = tuple(item for item in components if item.market == "US")
    hk_entry = datetime.combine(pcf_day, time(14, 50), HK_TZ)
    hk_close = datetime.combine(pcf_day, time(15, 59), HK_TZ)
    us_open = datetime.combine(pcf_day, time(9, 30), US_TZ)
    us_close = datetime.combine(pcf_day, time(15, 59), US_TZ)
    hk_open_value = _portfolio_value_at(hk_components, raw_bars, hk_entry, settlement_fx)
    hk_close_value = _portfolio_value_at(hk_components, raw_bars, hk_close, settlement_fx)
    us_open_value = _portfolio_value_at(us_components, raw_bars, us_open, settlement_fx)
    us_close_value = _portfolio_value_at(us_components, raw_bars, us_close, settlement_fx)
    kweb_open = _price_at_or_before(raw_bars.get("US_KWEB", []), us_open)
    kweb_close = _price_at_or_before(raw_bars.get("US_KWEB", []), us_close)
    adr_return = _pct_change(us_open_value, us_close_value)
    kweb_return = _pct_change(kweb_open, kweb_close)
    one_day_ratio = adr_return / kweb_return if adr_return is not None and kweb_return not in (None, Decimal("0")) else None
    return {
        "hk_entry_value": hk_open_value,
        "hk_close_value": hk_close_value,
        "hk_return_pct": _pct_change(hk_open_value, hk_close_value),
        "adr_open_value": us_open_value,
        "adr_close_value": us_close_value,
        "adr_return_pct": adr_return,
        "kweb_open": kweb_open,
        "kweb_close": kweb_close,
        "kweb_return_pct": kweb_return,
        "one_day_adr_kweb_ratio": one_day_ratio,
    }


def _pct(value: Decimal | None) -> str:
    return "不可用" if value is None else f"{value:.3f}%"


def _last_valid_quote(rows: Iterable[dict[str, object]]) -> tuple[datetime, Decimal, Decimal] | None:
    candidates: list[tuple[datetime, Decimal, Decimal]] = []
    for row in rows:
        try:
            timestamp = datetime.fromisoformat(str(row["timestamp_utc"])).astimezone(UTC)
            bid = _decimal(str(row["bid_price"]), "bid")
            ask = _decimal(str(row["ask_price"]), "ask")
        except (ValueError, KeyError):
            continue
        if bid > 0 and ask >= bid:
            candidates.append((timestamp, bid, ask))
    return max(candidates, key=lambda item: item[0]) if candidates else None


def _quote_execution_sample(
    *,
    components: tuple[PcfComponent, ...],
    raw_bars: dict[str, list[dict[str, object]]],
    raw_ticks: dict[str, list[dict[str, object]]],
    settlement_fx: CfetsSettlementFx,
) -> dict[str, Decimal | datetime | None]:
    """Simulate the US leg with executable quotes: short at Bid, cover at Ask.

    ``open`` tick files intentionally end around 09:35 ET, avoiding the first
    minutes' opening auction disorder. ``close`` files end immediately before
    16:00 ET. This is a single historical sample, not a fitted hedge.
    """
    short_proceeds = Decimal("0")
    cover_cost = Decimal("0")
    open_ages: list[Decimal] = []
    close_ages: list[Decimal] = []
    for component in (item for item in components if item.market == "US"):
        entry = _last_valid_quote(raw_ticks.get(f"US_{component.code}_open", []))
        exit_quote = _last_valid_quote(raw_ticks.get(f"US_{component.code}_close", []))
        if entry is None or exit_quote is None:
            return {"short_proceeds": None, "cover_cost": None, "pnl": None}
        entry_time, entry_bid, _ = entry
        exit_time, _, exit_ask = exit_quote
        short_proceeds += component.shares * entry_bid * settlement_fx.usd_cny
        cover_cost += component.shares * exit_ask * settlement_fx.usd_cny
        entry_anchor = datetime.combine(entry_time.astimezone(US_TZ).date(), time(9, 35), US_TZ).astimezone(UTC)
        exit_anchor = datetime.combine(exit_time.astimezone(US_TZ).date(), time(16, 0), US_TZ).astimezone(UTC)
        open_ages.append(Decimal(str((entry_anchor - entry_time).total_seconds())))
        close_ages.append(Decimal(str((exit_anchor - exit_time).total_seconds())))

    kweb_entry = _last_valid_quote(raw_ticks.get("US_KWEB_open", []))
    kweb_exit = _last_valid_quote(raw_ticks.get("US_KWEB_close", []))
    result: dict[str, Decimal | datetime | None] = {
        "short_proceeds": short_proceeds,
        "cover_cost": cover_cost,
        "pnl": short_proceeds - cover_cost,
        "max_open_quote_age_seconds": max(open_ages) if open_ages else None,
        "max_close_quote_age_seconds": max(close_ages) if close_ages else None,
        "kweb_entry_bid": kweb_entry[1] if kweb_entry else None,
        "kweb_exit_ask": kweb_exit[2] if kweb_exit else None,
        "kweb_entry_time": kweb_entry[0] if kweb_entry else None,
        "kweb_exit_time": kweb_exit[0] if kweb_exit else None,
        "kweb_us_leg_one_beta_shares": None,
    }
    if kweb_entry is not None:
        if kweb_entry[1] > 0:
            result["kweb_us_leg_one_beta_shares"] = short_proceeds / (kweb_entry[1] * settlement_fx.usd_cny)
    return result


def build_strategy_report(
    *,
    pcf_day: date,
    components: tuple[PcfComponent, ...],
    summaries: Iterable[SessionSummary],
    errors: list[dict[str, str]],
    raw_bars: dict[str, list[dict[str, object]]],
    raw_ticks: dict[str, list[dict[str, object]]],
    settlement_fx: CfetsSettlementFx,
) -> str:
    """Return a research report that explicitly excludes both cash-component lines."""
    by_key = _summary_map(summaries)

    hk_value = Decimal("0")
    us_value = Decimal("0")
    unavailable: list[str] = []
    direct_lines: list[str] = []
    for component in components:
        summary = by_key.get(f"{component.market}_{component.code}")
        price = summary.close_window_vwap if summary else None
        if price is None:
            unavailable.append(component.code)
            continue
        if component.market == "HK":
            notional = component.shares * price * settlement_fx.hkd_cny
            hk_value += notional
        else:
            notional = component.shares * price * settlement_fx.usd_cny
            us_value += notional
        direct_lines.append(
            f"- `{component.code}` {component.symbol}：空 {component.shares:,.0f} 股，"
            f"TWS 收盘窗口价格 {price} {component.currency}，估计名义 {_value(notional)} CNY"
        )

    total_value = hk_value + us_value if not unavailable else None
    kweb = by_key.get("US_KWEB")
    kweb_price = kweb.close_window_vwap if kweb else None
    proxy_shares: Decimal | None = None
    if total_value is not None and kweb_price and kweb_price > 0:
        proxy_shares = total_value / settlement_fx.usd_cny / kweb_price
    timing = _timing_sample(
        pcf_day=pcf_day,
        components=components,
        raw_bars=raw_bars,
        settlement_fx=settlement_fx,
    )
    executable = _quote_execution_sample(
        components=components,
        raw_bars=raw_bars,
        raw_ticks=raw_ticks,
        settlement_fx=settlement_fx,
    )

    lines = [
        "# 159605 TWS 证券腿研究报告",
        "",
        f"PCF 日期：`{pcf_day:%Y-%m-%d}`。本报告只研究 PCF 的股票现金替代腿，"
        "**刻意不计入预估现金差额、实际现金差额及实际现金替代退款**。因此不是最终赎回收益或下单指令。",
        "",
        "## TWS 数据覆盖",
        "",
        f"- PCF 成分：{sum(item.market == 'HK' for item in components)} 只港股、"
        f"{sum(item.market == 'US' for item in components)} 只美国 ADR；",
        "- 原始 1 分钟 `TRADES` 保存在 `bars/`；",
        "- KWEB 与 7 只 ADR 的开盘/收盘前真实 Bid/Ask tick 保存在 `ticks/`；",
        f"- 外汇交易中心 T 日收盘价：USD/CNY `{settlement_fx.usd_cny}` "
        f"（{settlement_fx.usd_close_time}），HKD/CNY `{settlement_fx.hkd_cny}` "
        f"（{settlement_fx.hkd_close_time}）；未使用 IB 离岸汇率。",
        "",
        "## 收盘窗口的 PCF 证券腿估值（未含任何现金线）",
        "",
        f"- 港股腿：{_value(hk_value)} CNY；",
        f"- ADR 腿：{_value(us_value)} CNY；",
            f"- 合计：{_value(total_value)} CNY。",
    ]
    if unavailable:
        lines.append(f"- 无法估值的成分 / 输入：`{', '.join(unavailable)}`；合计不可视为完整篮子。")
    lines.extend(
        [
            "",
            "## 本样本的开平仓时段观察",
            "",
            f"- 港股 PCF 腿，14:50→15:59 HKT：{_value(timing['hk_entry_value'])} → "
            f"{_value(timing['hk_close_value'])} CNY，变动 {_pct(timing['hk_return_pct'])}；",
            f"- ADR PCF 腿，09:30→15:59 ET：{_value(timing['adr_open_value'])} → "
            f"{_value(timing['adr_close_value'])} CNY，变动 {_pct(timing['adr_return_pct'])}；",
            f"- KWEB，09:30→15:59 ET：{_value(timing['kweb_open'])} → "
            f"{_value(timing['kweb_close'])} USD，变动 {_pct(timing['kweb_return_pct'])}；",
            f"- ADR / KWEB 单日收益率比：{_value(timing['one_day_adr_kweb_ratio'])}。"
            "这只是单日结果，不能作为 beta 或下单比例。",
            "",
            "这组时段数据支持将港股和 ADR 拆开：港股风险可在 A 股收盘前后处理；"
            "ADR 风险只能在美股开盘后开始处理。KWEB 美股开盘到收盘的单日变化可辅助观察 ADR beta，"
            "却无法覆盖 A 股成交后到美股开盘前的空窗。",
            "",
            "## 美股夜间时段：可执行 Bid/Ask 样本",
            "",
            "以下金额不使用 `TRADES` 成交价，而是对 7 只 ADR 按“开空取 Bid、回补取 Ask”计算；"
            "开仓报价取 09:35 ET 前的最后有效报价，平仓报价取 16:00 ET 前的最后有效报价。",
            f"- ADR 空头卖出所得：{_value(executable['short_proceeds'])} CNY；",
            f"- ADR 空头回补成本：{_value(executable['cover_cost'])} CNY；",
            f"- 该单日、该报价窗口的 ADR 空头 P&L：{_value(executable['pnl'])} CNY；",
            f"- 最旧的开仓 / 平仓报价距窗口锚点：{_value(executable['max_open_quote_age_seconds'])} / "
            f"{_value(executable['max_close_quote_age_seconds'])} 秒；",
            f"- KWEB 同样口径的开空 Bid / 回补 Ask：{_value(executable['kweb_entry_bid'])} / "
            f"{_value(executable['kweb_exit_ask'])} USD；",
            f"- 以 ADR 腿名义价值、1.00 beta 粗略换算的 KWEB 数量："
            f"{_value(executable['kweb_us_leg_one_beta_shares'])} 股。",
            "",
            "报价新鲜度或点差不满足预设风控阈值时，必须标为不可执行；尤其是盘前、盘后或其他稀疏时段，"
            "不得用最后一笔成交价补齐估值。TRADES 数据仅用于走势、成交量和 beta 回测。",
            "",
            "## 推测的开平仓设计",
            "",
            "### A. 主方案：PCF 精确篮子（推荐研究基准）",
            "",
            "1. 中国盘中只在折价已覆盖费用、借券、汇率和执行压力测试后买入 159605 并提交赎回；",
            "2. 港股 23 只：按 PCF 原始股数建立空头，尽量在 15:50 前完成；在 15:58–16:00 HKT 用与基金结算参考相近的窗口回补；",
            "3. ADR 7 只：在同一 T 日美国常规开盘后，以可成交 Bid 建立空头，并在 15:58–16:00 ET 以 Ask 回补；",
            "4. 记录每只股票的真实 Bid/Ask、成交价、借券费和未成交量。基金实际卖出时点未知，"
            "故这只是在复制“未卖出证券按收盘价结算”的兜底参考，仍存在执行基差。",
            "",
            "这会留下一个不可消除的时段：A 股买入后的美国开盘前风险。常规时段 KWEB 与 A 股不重叠，"
            "因此不能把这段风险称为已锁定。",
            "",
            "### B. KWEB 代理（仅作残余 beta，不与 A 全量叠加）",
            "",
            f"- KWEB 收盘窗口价格：{_value(kweb_price)} USD；",
            f"- 以 1.00 beta、全证券腿等美元名义换算的粗略数量：{_value(proxy_shares)} 股 KWEB；",
            f"- 若港股已用精确成分股在 16:00 HKT 平掉，美股时段只代理 ADR 腿时的粗略数量："
            f"{_value(executable['kweb_us_leg_one_beta_shares'])} 股 KWEB。",
            "",
            "上面的 KWEB 数量仅是名义价值换算，**不是订单数量**：KWEB 的持仓、权重、港美上市版本、"
            "费用及交易时点均不与该日 PCF 完全一致。实际使用前，必须用保存下来的分钟数据回归每个时段的 beta；"
            "若采用 KWEB，则只对未由精确成分股覆盖的残余风险使用，禁止与 30 只股票全额重复做空。",
            "",
            "### C. 下一步回测",
            "",
            "使用多个 PCF 日重复运行本脚本，计算：港股收盘到美股开盘、美股开盘到收盘、"
            "以及全日三个窗口中 PCF 证券腿对 KWEB 的收益 beta、残差 P95 和平均实际 Bid/Ask 点差。"
            "仅当折价大于这些压力项与全部费用之和，才进入人工复核。",
            "",
            "## 精确篮子目标数量",
            "",
            *direct_lines,
        ]
    )
    if errors:
        lines.extend(["", "## TWS 数据错误（必须处理后才可使用）", ""])
        lines.extend(f"- `{row['key']}` / {row['stage']}：{row['error']}" for row in errors)
    return "\n".join(lines) + "\n"


def run_research(
    *,
    pcf_day: date,
    cache_root: Path,
    output_dir: Path,
    host: str,
    port: int,
    client_id: int,
    pace_seconds: float,
    fx_csv: Path,
    refresh_cfets: bool,
    resume: bool = False,
) -> Path:
    source_path = pcf_path(pcf_day, cache_root)
    if not source_path.exists():
        raise FileNotFoundError(f"PCF 不存在：{source_path}")
    components = load_pcf_components(source_path)
    specs = build_contract_specs(components)
    settlement_fx = load_cfets_settlement_fx(
        trading_day=pcf_day,
        csv_path=fx_csv,
        refresh=refresh_cfets,
    )
    if output_dir.exists():
        if not resume:
            raise FileExistsError(f"研究目录已存在；若要续传请加 --resume：{output_dir}")
        if not output_dir.is_dir():
            raise NotADirectoryError(f"研究输出路径不是目录：{output_dir}")
    else:
        output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "用途说明.md").write_text(
        "# 此目录的用途\n\n"
        "这是 `research_159605_tws.py` 生成的 **159605 TWS 只读研究数据**。\n\n"
        "- 用于复核 PCF 成分股、KWEB 的历史行情与对冲时段；\n"
        "- 人民币换算固定使用外汇交易中心 T 日 USD/CNY、HKD/CNY 收盘价，不使用 IB 离岸汇率；\n"
        "- 仅研究证券现金替代腿；不包含预估现金差额、实际现金差额或实际现金替代退款；\n"
        "- `strategy_report.md` 是研究推测，不是订单或最终赎回收益；\n"
        "- 本目录不属于正式账务、校准数据或网站运行数据；确认无用后可整体删除。\n",
        encoding="utf-8",
    )
    (output_dir / "pcf_159605.xml").write_bytes(source_path.read_bytes())
    _write_csv(
        output_dir / "cfets_t_day_close.csv",
        ("trade_date", "source", "pair", "quote_time", "close_time", "rate"),
        (
            {
                "trade_date": settlement_fx.trade_date.isoformat(),
                "source": settlement_fx.source,
                "pair": "USD/CNY",
                "quote_time": "CLOSE",
                "close_time": settlement_fx.usd_close_time,
                "rate": format(settlement_fx.usd_cny, "f"),
            },
            {
                "trade_date": settlement_fx.trade_date.isoformat(),
                "source": settlement_fx.source,
                "pair": "HKD/CNY",
                "quote_time": "CLOSE",
                "close_time": settlement_fx.hkd_close_time,
                "rate": format(settlement_fx.hkd_cny, "f"),
            },
        ),
    )
    _write_csv(
        output_dir / "pcf_components.csv",
        ("code", "symbol", "shares", "market", "currency", "source"),
        (
            {
                "code": item.code,
                "symbol": item.symbol,
                "shares": format(item.shares, "f"),
                "market": item.market,
                "currency": item.currency,
                "source": item.source,
            }
            for item in components
        ),
    )

    summaries: list[SessionSummary] = []
    raw_bars: dict[str, list[dict[str, object]]] = {}
    raw_ticks: dict[str, list[dict[str, object]]] = {}
    with TwsResearchFetcher(host=host, port=port, client_id=client_id, pace_seconds=pace_seconds) as fetcher:
        for index, spec in enumerate(specs, start=1):
            bar_path = output_dir / "bars" / f"{spec.key.lower()}_1m.csv"
            try:
                timezone = HK_TZ if spec.market == "HK" else US_TZ
                if resume and bar_path.is_file():
                    rows = _read_csv(bar_path)
                    source = "本地续传"
                else:
                    bars = fetcher.bars(spec, pcf_day)
                    rows = bar_rows(bars, timezone)
                    _write_csv(
                        bar_path,
                        ("timestamp_local", "timestamp_utc", "open", "high", "low", "close", "volume", "average", "bar_count"),
                        rows,
                    )
                    source = "已拉取"
                summaries.append(summarise_bars(spec, pcf_day, rows))
                raw_bars[spec.key] = rows
                print(f"[{index}/{len(specs)}] {source} {spec.key}：{len(rows)} 根 1 分钟线", flush=True)
            except Exception as exc:  # persist incomplete pulls for diagnosis
                fetcher.record_error(spec, "bars", exc)
                print(f"[{index}/{len(specs)}] {spec.key} 失败：{exc}", flush=True)

        # True bid/ask ticks have a much stricter pacing budget. Capture only
        # KWEB and the exact seven ADRs, at US open and close reference windows.
        for spec in (item for item in specs if item.market == "US"):
            for marker in ("open", "close"):
                tick_path = output_dir / "ticks" / f"{spec.key.lower()}_{marker}_bid_ask.csv"
                try:
                    if resume and tick_path.is_file():
                        rows = _read_csv(tick_path)
                        source = "本地续传"
                    else:
                        ticks = fetcher.bid_ask_ticks(spec, pcf_day, marker)
                        rows = tick_rows(ticks, US_TZ)
                        _write_csv(
                            tick_path,
                            ("timestamp_local", "timestamp_utc", "bid_price", "ask_price", "bid_size", "ask_size", "bid_past_low", "ask_past_high"),
                            rows,
                        )
                        source = "已拉取"
                    raw_ticks[f"{spec.key}_{marker}"] = rows
                    print(f"[ticks] {source} {spec.key} {marker}：{len(rows)} 条 Bid/Ask", flush=True)
                except Exception as exc:
                    fetcher.record_error(spec, f"bid_ask_{marker}", exc)
                    print(f"[ticks] {spec.key} {marker} 失败：{exc}", flush=True)
        errors = list(fetcher.errors)

    _write_csv(output_dir / "session_summary.csv", tuple(SessionSummary.__dataclass_fields__), (
        {
            "key": item.key,
            "market": item.market,
            "currency": item.currency,
            "shares": _stringify_decimal(item.shares),
            "trade_date": item.trade_date,
            "open": _stringify_decimal(item.open),
            "close": _stringify_decimal(item.close),
            "close_window_vwap": _stringify_decimal(item.close_window_vwap),
            "close_window_volume": _stringify_decimal(item.close_window_volume),
            "bar_count": item.bar_count,
            "source": item.source,
        }
        for item in summaries
    ))
    _write_csv(output_dir / "errors.csv", ("key", "symbol", "stage", "error"), errors)
    (output_dir / "strategy_report.md").write_text(
        build_strategy_report(
            pcf_day=pcf_day,
            components=components,
            summaries=summaries,
            errors=errors,
            raw_bars=raw_bars,
            raw_ticks=raw_ticks,
            settlement_fx=settlement_fx,
        ),
        encoding="utf-8",
    )
    manifest = {
        "created_at": datetime.now(UTC).isoformat(),
        "pcf_day": pcf_day.isoformat(),
        "pcf_source": str(source_path),
        "mode": "read_only_tws_research",
        "cash_lines": "excluded: EstimateCashComponent, CashComponent, actual cash-substitution refund",
        "tws": {"host": host, "port": port, "client_id": client_id, "readonly": True, "pace_seconds": pace_seconds},
        "fx": {
            "source": settlement_fx.source,
            "trade_date": settlement_fx.trade_date.isoformat(),
            "usd_cny_close": format(settlement_fx.usd_cny, "f"),
            "hkd_cny_close": format(settlement_fx.hkd_cny, "f"),
            "usd_close_time": settlement_fx.usd_close_time,
            "hkd_close_time": settlement_fx.hkd_close_time,
            "ib_offshore_fx_used": False,
        },
        "components": [asdict(item) | {"shares": format(item.shares, "f")} for item in components],
        "files": [str(path.relative_to(output_dir)) for path in sorted(output_dir.rglob("*")) if path.is_file()],
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return output_dir


def main() -> int:
    parser = argparse.ArgumentParser(description="只读拉取 159605 PCF/TWS 数据到项目内临时研究目录")
    parser.add_argument("--pcf-date", type=date.fromisoformat, help="PCF 日期，默认使用最新本地缓存")
    parser.add_argument("--cache-root", type=Path, default=CACHE_ROOT)
    parser.add_argument("--fx-csv", type=Path, default=ROOT / "fx_data" / "fx_rates.csv")
    parser.add_argument("--no-refresh-cfets", action="store_true", help="仅使用本地已缓存的CFETS收盘价")
    parser.add_argument("--output-dir", type=Path, help="默认在项目内 临时研究数据/ 创建唯一文件夹")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=90605, help="独立只读 client id，避免占用网站使用的 8888")
    parser.add_argument("--pace-seconds", type=float, default=MIN_PACING_SECONDS)
    parser.add_argument("--resume", action="store_true", help="续传已有研究目录，跳过已落盘的 1 分钟线")
    args = parser.parse_args()

    cache_root = args.cache_root.expanduser().resolve()
    pcf_day = args.pcf_date or latest_cached_pcf_date(cache_root)
    output_dir = args.output_dir.expanduser().resolve() if args.output_dir else default_output_dir(pcf_day)
    destination = run_research(
        pcf_day=pcf_day,
        cache_root=cache_root,
        output_dir=output_dir,
        host=args.host,
        port=args.port,
        client_id=args.client_id,
        pace_seconds=args.pace_seconds,
        fx_csv=args.fx_csv.expanduser().resolve(),
        refresh_cfets=not args.no_refresh_cfets,
        resume=args.resume,
    )
    print(f"\n研究数据已写入：{destination}")
    print(f"策略报告：{destination / 'strategy_report.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
