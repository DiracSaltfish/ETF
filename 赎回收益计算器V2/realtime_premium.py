from __future__ import annotations

import math
import json
import os
import queue
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Callable
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

from PyQt5.QtCore import QObject, QTimer, pyqtSignal

import fx_rates
import xop_close_orders


# The real-time page prices the redeemable *total basket asset*, rather than
# only the stock refund leg.  The stock-only PCF/XOP equivalent from the
# confirmed settlement sample is 996 shares per 1,000,000 fund units.  The
# separately supplied PCF EstimateCashComponent is added below.
DEFAULT_XOP_SHARES = Decimal("996")
DEFAULT_REDEMPTION_UNIT = Decimal("1000000")
SINA_QUOTE_URL = "https://hq.sinajs.cn/list=sz159518"
SINA_HEADERS = {
    "Referer": "https://finance.sina.com.cn",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
}
SHANGHAI = ZoneInfo("Asia/Shanghai")
AUTO_CONNECT_START = clock_time(9, 15)
AUTO_CONNECT_END = clock_time(15, 0)
AUTO_TWS_CLIENT_ID_BASE = 100_000
AUTO_TWS_CLIENT_ID_SPAN = 900_000
TWS_CLIENT_ID_ATTEMPTS = 12


def is_auto_connection_window(now: datetime | None = None) -> bool:
    current = now or datetime.now(SHANGHAI)
    local_time = current.timetz().replace(tzinfo=None)
    return current.weekday() < 5 and AUTO_CONNECT_START <= local_time < AUTO_CONNECT_END


def automatic_tws_client_id(process_id: int | None = None) -> int:
    """Return a stable, low-collision client ID for the current process."""
    pid = os.getpid() if process_id is None else int(process_id)
    return AUTO_TWS_CLIENT_ID_BASE + (abs(pid) % AUTO_TWS_CLIENT_ID_SPAN)


def tws_client_id_candidates(
    preferred_client_id: int,
    *,
    auto_allocate: bool = True,
    process_id: int | None = None,
    attempts: int = TWS_CLIENT_ID_ATTEMPTS,
) -> tuple[int, ...]:
    """Build the ordered client-ID attempts used for a TWS connection."""
    count = max(1, int(attempts))
    preferred = max(0, min(2_147_483_647, int(preferred_client_id)))
    auto_start = automatic_tws_client_id(process_id)
    candidates: list[int] = []
    if not auto_allocate:
        candidates.append(preferred)
    offset = 0
    while len(candidates) < count:
        candidate = AUTO_TWS_CLIENT_ID_BASE + (
            (auto_start - AUTO_TWS_CLIENT_ID_BASE + offset) % AUTO_TWS_CLIENT_ID_SPAN
        )
        if candidate not in candidates:
            candidates.append(candidate)
        offset += 1
    return tuple(candidates)


REALTIME_SCHEMA_DOCUMENT = """# realtime.json 字段说明

## 文件位置

程序按北京时间写入：`共享文件夹目录路径/YYYYMMDD/realtime.json`。

文件采用 UTF-8 编码、JSON 缩进格式和原子替换写入。其他程序不会读到只写了一部分的 JSON。

## 更新规则

当 XOP TWS Bid/Ask、159518 新浪五档或 CFETS 汇率任一数据更新，并且三项数据均完整时，程序会覆盖写入最新快照。
集合竞价阶段如果新浪只返回买一/卖一有效价格，JSON 仍保持 `bid2`、`bid1`、`ask1`、`ask2` 四个数值档位；
缺失的同侧二档会复用买一/卖一价格，确保下游显示器持续刷新。

## 核心口径

- 一个篮子使用 **996 股 XOP 的证券资产等价**，对应 1,000,000 份 159518。
- 另加当日 159518 PCF 的 `EstimateCashComponent`，它是同一最小申赎单位的人民币预估现金差额。
- `basket.bid_cny` = 996 × XOP Bid × CFETS USD/CNY + PCF EstimateCashComponent。
- `basket.ask_cny` = 996 × XOP Ask × CFETS USD/CNY + PCF EstimateCashComponent。
- `nav.bid` = `basket.bid_cny` ÷ 1,000,000；`nav.ask` 同理。
- `premium_rate_vs_basket_bid_nav` = 国内盘口价格 ÷ `nav.bid` - 1。
- `premium_rate_vs_basket_ask_nav` = 国内盘口价格 ÷ `nav.ask` - 1。
- 为兼容旧读取程序，`premium_rate_vs_xop_*_nav` 仍保留为同值别名；它们在 schema 2 中同样指向总篮子资产净值。
- `premium_percent_*` 是对应比率乘以 100 后的百分数；例如 `0.12` 表示 0.12%。

这是一项中国盘中可观察的总资产代理值；并不声称当日美股收盘时基金管理人实际成交的逐笔价格已经已知。

## 字段结构

- `schema_version`：当前为 2。
- `generated_at`：本次文件生成时间，ISO 8601，北京时间。
- `trade_date`：生成日期，格式 YYYYMMDD。
- `symbol` / `fund_name`：国内基金代码和名称。
- `valuation.xop`：XOP Bid、Ask、Last、行情类型和行情接收时间。
- `valuation.fx`：CFETS USD/CNY 汇率、实际交易日和报价时点。
- `valuation.pcf`：PCF 交易日及纳入估值的 `EstimateCashComponent`。
- `valuation.stock_component`：XOP 等价证券资产的人民币值，不含现金差额。
- `valuation.basket`：证券资产加预估现金差额后的总篮子人民币估值。
- `valuation.nav`：按 XOP Bid/Ask 分别计算的每份总篮子预估净值。
- `order_book.bid2`：国内买二；集合竞价仅一档时复用买一。
- `order_book.bid1`：国内买一。
- `order_book.ask1`：国内卖一。
- `order_book.ask2`：国内卖二；集合竞价仅一档时复用卖一。
- 每个盘口档位包含价格和两套溢折价率，不包含盘口数量。

`premium_rate_*` 和 `premium_percent_*` 均为 JSON 数值，不是带 `%` 的字符串，便于其他程序直接计算。
"""


@dataclass(frozen=True)
class QuoteLevel:
    price: Decimal
    volume: int


@dataclass(frozen=True)
class SinaQuote:
    symbol: str
    name: str
    bid: Decimal
    ask: Decimal
    bid_volume: int
    ask_volume: int
    last: Decimal
    market_time: datetime | None
    received_at: datetime
    bids: tuple[QuoteLevel, ...] = ()
    asks: tuple[QuoteLevel, ...] = ()


@dataclass(frozen=True)
class CfetsQuote:
    rate: Decimal
    trading_day: date
    quote_time: str
    fetched_at: str


@dataclass(frozen=True)
class XopQuote:
    bid: Decimal | None
    ask: Decimal | None
    last: Decimal | None
    received_at: datetime
    market_data_type: str = "Live"


@dataclass(frozen=True)
class PremiumValuation:
    shares: Decimal
    fx_rate: Decimal
    estimate_cash_component_cny: Decimal
    xop_bid: Decimal
    xop_ask: Decimal
    domestic_bid: Decimal
    domestic_ask: Decimal
    stock_component_bid_cny: Decimal
    stock_component_ask_cny: Decimal
    basket_bid_cny: Decimal
    basket_ask_cny: Decimal
    nav_bid: Decimal
    nav_ask: Decimal
    domestic_bid_vs_xop_bid: Decimal
    domestic_ask_vs_xop_ask: Decimal
    executable_sell_domestic_vs_xop_ask: Decimal
    executable_buy_domestic_vs_xop_bid: Decimal
    pcf_trading_day: date | None = None


def _json_number(value: Decimal | None, places: int = 10) -> float | None:
    if value is None:
        return None
    return float(round(value, places))


def _book_level_payload(
    level: QuoteLevel | None,
    nav_bid: Decimal,
    nav_ask: Decimal,
) -> dict[str, float | None]:
    if level is None or level.price <= 0:
        return {
            "price": None,
            "premium_rate_vs_basket_bid_nav": None,
            "premium_percent_vs_basket_bid_nav": None,
            "premium_rate_vs_basket_ask_nav": None,
            "premium_percent_vs_basket_ask_nav": None,
            "premium_rate_vs_xop_bid_nav": None,
            "premium_percent_vs_xop_bid_nav": None,
            "premium_rate_vs_xop_ask_nav": None,
            "premium_percent_vs_xop_ask_nav": None,
        }
    bid_rate = level.price / nav_bid - Decimal("1")
    ask_rate = level.price / nav_ask - Decimal("1")
    return {
        "price": _json_number(level.price, 6),
        "premium_rate_vs_basket_bid_nav": _json_number(bid_rate),
        "premium_percent_vs_basket_bid_nav": _json_number(bid_rate * Decimal("100"), 8),
        "premium_rate_vs_basket_ask_nav": _json_number(ask_rate),
        "premium_percent_vs_basket_ask_nav": _json_number(ask_rate * Decimal("100"), 8),
        # Kept as schema-1 aliases for downstream readers.  In schema 2 the
        # nav includes the PCF cash component and represents total basket assets.
        "premium_rate_vs_xop_bid_nav": _json_number(bid_rate),
        "premium_percent_vs_xop_bid_nav": _json_number(bid_rate * Decimal("100"), 8),
        "premium_rate_vs_xop_ask_nav": _json_number(ask_rate),
        "premium_percent_vs_xop_ask_nav": _json_number(ask_rate * Decimal("100"), 8),
    }


def _top_two_levels_for_export(
    levels: tuple[QuoteLevel, ...],
    best_price: Decimal,
    best_volume: int,
) -> tuple[QuoteLevel | None, QuoteLevel | None]:
    positive_levels = [level for level in levels if level.price > 0]
    if not positive_levels and best_price > 0:
        positive_levels.append(QuoteLevel(best_price, best_volume))
    if len(positive_levels) == 1:
        positive_levels.append(positive_levels[0])
    first = positive_levels[0] if len(positive_levels) >= 1 else None
    second = positive_levels[1] if len(positive_levels) >= 2 else None
    return first, second


def build_realtime_payload(
    xop_quote: XopQuote,
    domestic_quote: SinaQuote,
    cfets_quote: CfetsQuote,
    valuation: PremiumValuation,
    *,
    generated_at: datetime | None = None,
) -> dict[str, object]:
    now = generated_at or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    bids = domestic_quote.bids
    asks = domestic_quote.asks
    bid1, bid2 = _top_two_levels_for_export(bids, domestic_quote.bid, domestic_quote.bid_volume)
    ask1, ask2 = _top_two_levels_for_export(asks, domestic_quote.ask, domestic_quote.ask_volume)
    return {
        "schema_version": 2,
        "generated_at": now.isoformat(timespec="milliseconds"),
        "trade_date": now.strftime("%Y%m%d"),
        "symbol": "SZ159518",
        "fund_name": domestic_quote.name or "标普油气",
        "valuation": {
            "xop_shares_per_basket": _json_number(valuation.shares, 4),
            "redemption_unit": int(DEFAULT_REDEMPTION_UNIT),
            "xop": {
                "symbol": "XOP",
                "bid": _json_number(xop_quote.bid, 6),
                "ask": _json_number(xop_quote.ask, 6),
                "last": _json_number(xop_quote.last, 6),
                "market_data_type": xop_quote.market_data_type,
                "received_at": xop_quote.received_at.isoformat(timespec="milliseconds"),
            },
            "fx": {
                "source": "CFETS",
                "pair": "USD/CNY",
                "rate": _json_number(cfets_quote.rate, 8),
                "trading_day": cfets_quote.trading_day.isoformat(),
                "quote_time": cfets_quote.quote_time,
            },
            "pcf": {
                "field": "EstimateCashComponent",
                "estimate_cash_component_cny": _json_number(valuation.estimate_cash_component_cny, 2),
                "trading_day": valuation.pcf_trading_day.isoformat() if valuation.pcf_trading_day else None,
            },
            "stock_component": {
                "bid_cny": _json_number(valuation.stock_component_bid_cny, 2),
                "ask_cny": _json_number(valuation.stock_component_ask_cny, 2),
            },
            "basket": {
                "bid_cny": _json_number(valuation.basket_bid_cny, 2),
                "ask_cny": _json_number(valuation.basket_ask_cny, 2),
            },
            "nav": {
                "bid": _json_number(valuation.nav_bid, 10),
                "ask": _json_number(valuation.nav_ask, 10),
            },
        },
        "order_book": {
            "bid2": _book_level_payload(bid2, valuation.nav_bid, valuation.nav_ask),
            "bid1": _book_level_payload(bid1, valuation.nav_bid, valuation.nav_ask),
            "ask1": _book_level_payload(ask1, valuation.nav_bid, valuation.nav_ask),
            "ask2": _book_level_payload(ask2, valuation.nav_bid, valuation.nav_ask),
        },
    }


def _atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def write_schema_document(shared_root: Path | str, *, generated_at: datetime | None = None) -> Path:
    now = generated_at or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    day_directory = Path(shared_root).expanduser().resolve() / now.strftime("%Y%m%d")
    document_path = day_directory / "realtime字段说明.md"
    _atomic_write_text(document_path, REALTIME_SCHEMA_DOCUMENT)
    return document_path


def write_realtime_files(
    shared_root: Path | str,
    xop_quote: XopQuote,
    domestic_quote: SinaQuote,
    cfets_quote: CfetsQuote,
    valuation: PremiumValuation,
    *,
    generated_at: datetime | None = None,
) -> tuple[Path, Path]:
    now = generated_at or datetime.now(SHANGHAI)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SHANGHAI)
    day_directory = Path(shared_root).expanduser().resolve() / now.strftime("%Y%m%d")
    payload = build_realtime_payload(
        xop_quote,
        domestic_quote,
        cfets_quote,
        valuation,
        generated_at=now,
    )
    json_path = day_directory / "realtime.json"
    _atomic_write_text(json_path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    document_path = write_schema_document(shared_root, generated_at=now)
    return json_path, document_path


def calculate_premium_valuation(
    xop_bid: Decimal,
    xop_ask: Decimal,
    fx_rate: Decimal,
    domestic_bid: Decimal,
    domestic_ask: Decimal,
    *,
    shares: Decimal = DEFAULT_XOP_SHARES,
    estimate_cash_component_cny: Decimal = Decimal("0"),
    pcf_trading_day: date | None = None,
    redemption_unit: Decimal = DEFAULT_REDEMPTION_UNIT,
) -> PremiumValuation:
    values = {
        "XOP Bid": xop_bid,
        "XOP Ask": xop_ask,
        "CFETS汇率": fx_rate,
        "国内买一": domestic_bid,
        "国内卖一": domestic_ask,
        "XOP股数": shares,
        "申赎单位": redemption_unit,
    }
    for name, value in values.items():
        if value <= 0:
            raise ValueError(f"{name}必须大于0")
    estimate_cash_component_cny = Decimal(estimate_cash_component_cny)
    stock_component_bid_cny = shares * xop_bid * fx_rate
    stock_component_ask_cny = shares * xop_ask * fx_rate
    basket_bid_cny = stock_component_bid_cny + estimate_cash_component_cny
    basket_ask_cny = stock_component_ask_cny + estimate_cash_component_cny
    nav_bid = basket_bid_cny / redemption_unit
    nav_ask = basket_ask_cny / redemption_unit
    return PremiumValuation(
        shares=shares,
        fx_rate=fx_rate,
        estimate_cash_component_cny=estimate_cash_component_cny,
        xop_bid=xop_bid,
        xop_ask=xop_ask,
        domestic_bid=domestic_bid,
        domestic_ask=domestic_ask,
        stock_component_bid_cny=stock_component_bid_cny,
        stock_component_ask_cny=stock_component_ask_cny,
        basket_bid_cny=basket_bid_cny,
        basket_ask_cny=basket_ask_cny,
        nav_bid=nav_bid,
        nav_ask=nav_ask,
        domestic_bid_vs_xop_bid=domestic_bid / nav_bid - Decimal("1"),
        domestic_ask_vs_xop_ask=domestic_ask / nav_ask - Decimal("1"),
        executable_sell_domestic_vs_xop_ask=domestic_bid / nav_ask - Decimal("1"),
        executable_buy_domestic_vs_xop_bid=domestic_ask / nav_bid - Decimal("1"),
        pcf_trading_day=pcf_trading_day,
    )


def parse_sina_quote(payload: bytes, *, received_at: datetime | None = None) -> SinaQuote:
    text = payload.decode("gb18030", errors="replace").strip()
    marker = 'var hq_str_sz159518="'
    start = text.find(marker)
    if start < 0:
        raise ValueError("新浪响应中没有 sz159518")
    start += len(marker)
    end = text.find('"', start)
    if end < 0:
        raise ValueError("新浪响应格式不完整")
    fields = text[start:end].split(",")
    if len(fields) < 32:
        raise ValueError(f"新浪响应字段不足：{len(fields)}")

    def number(index: int, name: str) -> Decimal:
        try:
            value = Decimal(fields[index].strip())
        except Exception as exc:
            raise ValueError(f"新浪{name}无法解析") from exc
        if value < 0:
            raise ValueError(f"新浪{name}不能为负数")
        return value

    market_time = None
    day_text = fields[30].strip()
    time_text = fields[31].strip()
    if day_text and time_text:
        try:
            market_time = datetime.fromisoformat(f"{day_text}T{time_text}")
        except ValueError:
            market_time = None
    bids = tuple(
        QuoteLevel(number(price_index, f"买{level}价"), int(number(volume_index, f"买{level}量")))
        for level, (price_index, volume_index) in enumerate(
            ((11, 10), (13, 12), (15, 14), (17, 16), (19, 18)), start=1
        )
    )
    asks = tuple(
        QuoteLevel(number(price_index, f"卖{level}价"), int(number(volume_index, f"卖{level}量")))
        for level, (price_index, volume_index) in enumerate(
            ((21, 20), (23, 22), (25, 24), (27, 26), (29, 28)), start=1
        )
    )
    return SinaQuote(
        symbol="159518",
        name=fields[0].strip(),
        bid=number(11, "买一价"),
        ask=number(21, "卖一价"),
        bid_volume=int(number(10, "买一量")),
        ask_volume=int(number(20, "卖一量")),
        last=number(3, "最新价"),
        market_time=market_time,
        received_at=received_at or datetime.now(),
        bids=bids,
        asks=asks,
    )


def fetch_sina_quote(timeout: float = 8.0) -> SinaQuote:
    request = Request(SINA_QUOTE_URL, headers=SINA_HEADERS)
    with urlopen(request, timeout=timeout) as response:
        return parse_sina_quote(response.read())


def latest_cfets_quote(
    store: fx_rates.FxRateStore,
    *,
    as_of: date | None = None,
    refresh: bool = True,
    lookback_days: int = 10,
) -> CfetsQuote:
    current = as_of or datetime.now(SHANGHAI).date()
    errors: list[str] = []
    for offset in range(lookback_days + 1):
        candidate = current - timedelta(days=offset)
        if candidate.weekday() >= 5:
            continue
        if refresh:
            try:
                store.refresh_cfets_date(candidate)
            except Exception as exc:
                errors.append(f"{candidate:%Y-%m-%d}: {exc}")
        records = [
            row
            for row in store.load_day_records(candidate)
            if row.get("source") == fx_rates.CFETS_SOURCE
            and row.get("pair") == fx_rates.DISPLAY_PAIR
            and str(row.get("quote_time") or "").endswith(":00")
        ]
        if not records:
            continue
        row = max(records, key=lambda item: str(item.get("quote_time") or ""))
        return CfetsQuote(
            rate=Decimal(str(row["rate"])),
            trading_day=candidate,
            quote_time=str(row["quote_time"]),
            fetched_at=str(row.get("fetched_at") or ""),
        )
    suffix = f"；{' | '.join(errors[-2:])}" if errors else ""
    raise ValueError(f"最近{lookback_days}天没有可用的CFETS美元人民币参考价{suffix}")


class SinaPollingClient(QObject):
    quoteUpdated = pyqtSignal(object)
    statusChanged = pyqtSignal(str, bool)

    def __init__(self, fetcher: Callable[[], SinaQuote] = fetch_sina_quote, parent=None) -> None:
        super().__init__(parent)
        self.fetcher = fetcher
        self.timer = QTimer(self)
        self.timer.setInterval(3000)
        self.timer.timeout.connect(self.refresh)
        self._running = False
        self._inflight = False
        self._generation = 0

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._generation += 1
        self.timer.start()
        self.statusChanged.emit("新浪行情连接中", True)
        self.refresh()

    def is_running(self) -> bool:
        return self._running

    def restart(self) -> None:
        """Renew the polling generation after a transport failure."""
        self.stop()
        self.start()

    def stop(self) -> None:
        self._running = False
        self._generation += 1
        self.timer.stop()
        self.statusChanged.emit("新浪行情已停止", False)

    def refresh(self) -> None:
        if not self._running or self._inflight:
            return
        self._inflight = True
        generation = self._generation

        def work() -> None:
            try:
                quote = self.fetcher()
            except Exception as exc:
                if generation == self._generation:
                    self.statusChanged.emit(f"新浪行情失败：{exc}", False)
            else:
                if generation == self._generation:
                    self.quoteUpdated.emit(quote)
                    self.statusChanged.emit("新浪行情正常（3秒轮询）", True)
            finally:
                self._inflight = False

        threading.Thread(target=work, name="sina-159518", daemon=True).start()


class CfetsLatestClient(QObject):
    quoteUpdated = pyqtSignal(object)
    statusChanged = pyqtSignal(str, bool)

    def __init__(self, csv_path: Path | str, parent=None) -> None:
        super().__init__(parent)
        self.store = fx_rates.FxRateStore(csv_path)
        self.timer = QTimer(self)
        self.timer.setInterval(60_000)
        self.timer.timeout.connect(self.refresh)
        self._running = False
        self._inflight = False
        self._generation = 0

    def start(self) -> None:
        if not self._running:
            self._running = True
            self._generation += 1
            self.timer.start()
        self.refresh()

    def stop(self) -> None:
        self._running = False
        self._generation += 1
        self.timer.stop()

    def refresh(self) -> None:
        if self._inflight:
            return
        self._inflight = True
        generation = self._generation
        self.statusChanged.emit("正在刷新CFETS最新参考价", True)

        def work() -> None:
            try:
                quote = latest_cfets_quote(self.store)
            except Exception as exc:
                if generation == self._generation:
                    self.statusChanged.emit(f"CFETS刷新失败：{exc}", False)
            else:
                if generation == self._generation:
                    self.quoteUpdated.emit(quote)
                    self.statusChanged.emit(
                        f"CFETS已更新：{quote.trading_day:%Y-%m-%d} {quote.quote_time}", True
                    )
            finally:
                self._inflight = False

        threading.Thread(target=work, name="cfets-usdcny", daemon=True).start()


class TwsXopMarketData(QObject):
    quoteUpdated = pyqtSignal(object)
    statusChanged = pyqtSignal(str, bool)
    orderEvent = pyqtSignal(object)

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        parent=None,
        *,
        auto_client_id: bool = True,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self.port = port
        self.client_id = client_id
        self.auto_client_id = bool(auto_client_id)
        self.active_client_id: int | None = None
        self._lock = threading.Lock()
        self._ib = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._order_queue: queue.Queue[xop_close_orders.XopCloseOrderSpec] = queue.Queue()
        self._submitted_order_refs: set[str] = set()

    @staticmethod
    def build_contract():
        return xop_close_orders.build_xop_contract()

    def is_connected(self) -> bool:
        with self._lock:
            ib = self._ib
        return bool(ib is not None and ib.isConnected())

    def _emit_status(self, text: str, active: bool) -> None:
        try:
            self.statusChanged.emit(text, active)
        except RuntimeError:
            # The application may close while the background socket is winding
            # down. The QObject has then been deleted and there is no UI left to
            # update.
            pass

    def _emit_order_event(self, payload: dict[str, object]) -> None:
        try:
            self.orderEvent.emit(payload)
        except RuntimeError:
            pass

    def _emit_quote(self, quote: XopQuote) -> None:
        try:
            self.quoteUpdated.emit(quote)
        except RuntimeError:
            pass

    @staticmethod
    def _classify_ib_api_message(error_code: int, error_string: str) -> str:
        if error_code == 326:
            return "client_id_in_use"
        if error_code in {10275, 2104, 2106, 2107, 2108, 2158}:
            return "ignore"
        if error_code in {2103, 2105, 2157}:
            return "farm_disconnected"
        raw_text = str(error_string or "")
        text = raw_text.lower()
        if error_code == 399 and "09:30" in text and ("exchange" in text or "交易所" in raw_text):
            return "preopen_order_notice"
        if error_code == 1100:
            return "connection_lost"
        if error_code == 1101:
            return "connection_restored_data_lost"
        if error_code == 1102:
            return "connection_restored_data_maintained"
        return "error"

    def submit_confirmed_order(self, spec: xop_close_orders.XopCloseOrderSpec) -> bool:
        if not self.is_connected():
            self._emit_order_event(
                {"event": "rejected", "order_ref": spec.order_ref, "message": "IB未连接，订单未发送"}
            )
            return False
        if spec.order_ref in self._submitted_order_refs:
            self._emit_order_event(
                {"event": "rejected", "order_ref": spec.order_ref, "message": "orderRef已在本次会话发送，禁止重复"}
            )
            return False
        self._order_queue.put(spec)
        self._emit_order_event(
            {"event": "queued", "order_ref": spec.order_ref, "message": "已进入TWS发送队列"}
        )
        return True

    def connect_tws(self) -> None:
        host = self.host
        port = self.port
        preferred_client_id = self.client_id
        auto_client_id = self.auto_client_id
        candidates = tws_client_id_candidates(
            preferred_client_id,
            auto_allocate=auto_client_id,
        )

        def run() -> None:
            ib = None
            contract = None
            terminal_error = ""
            event_loop = None
            try:
                import asyncio

                event_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(event_loop)
                from ib_insync import IB

                for attempt_number, candidate_id in enumerate(candidates, start=1):
                    if self._stop_event.is_set():
                        return
                    candidate_ib = IB()
                    client_id_in_use = threading.Event()

                    def on_connect_error(
                        _req_id: int,
                        error_code: int,
                        _error_string: str,
                        _contract,
                        *,
                        attempted_id: int = candidate_id,
                        attempted_ib=candidate_ib,
                        conflict_event=client_id_in_use,
                    ) -> None:
                        if error_code != 326:
                            return
                        conflict_event.set()
                        self._emit_status(
                            f"IB client ID {attempted_id} 已占用，正在自动切换", True
                        )
                        # ib_insync normally waits for the full connection timeout
                        # after error 326. Wake that waiter so the next ID can be
                        # tried immediately; this IB instance is discarded below.
                        try:
                            attempted_ib.client.apiStart.emit()
                        except Exception:
                            pass

                    candidate_ib.errorEvent += on_connect_error
                    with self._lock:
                        self._ib = candidate_ib
                    if attempt_number > 1:
                        self._emit_status(
                            f"正在重试IB {host}:{port}，client ID {candidate_id}"
                            f"（{attempt_number}/{len(candidates)}）",
                            True,
                        )
                    try:
                        candidate_ib.wrapper.clientId = candidate_id
                        candidate_ib.client.connect(
                            host,
                            port,
                            candidate_id,
                            timeout=8,
                        )
                    except Exception:
                        if not client_id_in_use.is_set():
                            raise
                    finally:
                        candidate_ib.errorEvent -= on_connect_error

                    if client_id_in_use.is_set():
                        # TWS closes the socket at roughly the same time as it
                        # reports 326. IB.disconnect() first asks for connection
                        # statistics and can therefore raise "Not connected" in
                        # this race; Client.disconnect() is idempotent here.
                        try:
                            candidate_ib.client.disconnect()
                        except Exception:
                            pass
                        with self._lock:
                            if self._ib is candidate_ib:
                                self._ib = None
                        continue
                    if not candidate_ib.isConnected():
                        raise RuntimeError("TWS未建立连接")
                    ib = candidate_ib
                    with self._lock:
                        self.active_client_id = candidate_id
                    break

                if ib is None:
                    raise RuntimeError(
                        f"连续尝试 {len(candidates)} 个 client ID 均被占用"
                    )

                def on_error(req_id: int, error_code: int, error_string: str, _contract) -> None:
                    kind = self._classify_ib_api_message(error_code, error_string)
                    if kind == "ignore":
                        return
                    if kind == "farm_disconnected":
                        self._emit_status("IB行情服务短暂中断，等待TWS自动恢复", ib.isConnected())
                        return
                    if kind == "connection_lost":
                        self._emit_order_event(
                            {
                                "event": "warning",
                                "order_ref": "",
                                "order_id": req_id,
                                "status": "",
                                "message": "IBKR服务器连接中断；TWS正在自动恢复。请等待恢复后核对条件单状态。",
                            }
                        )
                        self._emit_status("IBKR服务器连接中断，TWS正在自动恢复", False)
                        return
                    if kind == "connection_restored_data_lost":
                        self._emit_order_event(
                            {
                                "event": "warning",
                                "order_ref": "",
                                "order_id": req_id,
                                "status": "",
                                "message": "IBKR连接已恢复，但行情订阅可能已丢失；请重新连接行情并核对条件单状态。",
                            }
                        )
                        self._emit_status("IBKR连接恢复，但行情订阅需要重新建立", False)
                        return
                    if kind == "connection_restored_data_maintained":
                        self._emit_order_event(
                            {
                                "event": "notice",
                                "order_ref": "",
                                "order_id": req_id,
                                "status": "",
                                "message": "IBKR连接已恢复，行情订阅已保留；已重新核对条件单状态。",
                            }
                        )
                        self._emit_status("IBKR连接已恢复，行情订阅已保留", True)
                        return
                    matching_trade = next(
                        (
                            trade
                            for trade in ib.trades()
                            if int(getattr(trade.order, "orderId", 0) or 0) == int(req_id)
                        ),
                        None,
                    )
                    order_ref = (
                        str(getattr(matching_trade.order, "orderRef", "") or "")
                        if matching_trade is not None else ""
                    )
                    if kind == "preopen_order_notice":
                        self._emit_order_event(
                            {
                                "event": "warning",
                                "order_ref": order_ref,
                                "order_id": req_id,
                                "status": "",
                                "message": "TWS提示：订单已预提交，将在美东09:30常规时段开始后下达交易所。",
                            }
                        )
                        return
                    self._emit_order_event(
                        {
                            "event": "error",
                            "order_ref": order_ref,
                            "order_id": req_id,
                            "status": "",
                            "message": f"IB错误 {error_code}：{error_string}",
                        }
                    )
                    self._emit_status(
                        f"IB错误 {error_code}（请求{req_id}）：{error_string}", ib.isConnected()
                    )

                ib.errorEvent += on_error

                def emit_trade_event(event_name: str, trade, message: str = "") -> None:
                    order_ref = str(getattr(trade.order, "orderRef", "") or "")
                    if not order_ref.startswith("XOP_REDEEM_CLOSE_"):
                        return
                    self._emit_order_event(
                        {
                            "event": event_name,
                            "order_ref": order_ref,
                            "order_id": int(getattr(trade.order, "orderId", 0) or 0),
                            "perm_id": int(getattr(trade.order, "permId", 0) or 0),
                            "status": str(getattr(trade.orderStatus, "status", "") or ""),
                            "filled": float(getattr(trade.orderStatus, "filled", 0) or 0),
                            "remaining": float(getattr(trade.orderStatus, "remaining", 0) or 0),
                            "message": message,
                        }
                    )

                ib.openOrderEvent += lambda trade: emit_trade_event("openOrder", trade)
                ib.orderStatusEvent += lambda trade: emit_trade_event("orderStatus", trade)
                ib.execDetailsEvent += lambda trade, fill: emit_trade_event(
                    "execDetails", trade, f"execution={getattr(fill.execution, 'execId', '')}"
                )
                ib.commissionReportEvent += lambda trade, fill, report: emit_trade_event(
                    "commissionReport", trade, f"commission={getattr(report, 'commission', '')}"
                )
                if self._stop_event.is_set():
                    return
                contract = self.build_contract()
                qualified = ib.qualifyContracts(contract)
                if not qualified:
                    raise RuntimeError("TWS无法确认XOP合约")
                contract = qualified[0]
                ib.reqMarketDataType(1)
                ticker = ib.reqMktData(contract, "", False, False)
                previous_timeout = ib.RequestTimeout
                try:
                    ib.RequestTimeout = 5
                    for open_trade in ib.reqAllOpenOrders():
                        existing_ref = str(getattr(open_trade.order, "orderRef", "") or "")
                        if existing_ref.startswith("XOP_REDEEM_CLOSE_"):
                            self._submitted_order_refs.add(existing_ref)
                except Exception as exc:
                    self._emit_order_event(
                        {"event": "warning", "order_ref": "", "message": f"读取现有条件单失败：{exc}"}
                    )
                finally:
                    ib.RequestTimeout = previous_timeout
                if self._stop_event.is_set():
                    return
                self._emit_status(
                    f"IB已连接（client ID {self.active_client_id}）并订阅XOP实时Bid/Ask；"
                    "周末无盘口时会显示--",
                    True,
                )
                previous = None
                while ib.isConnected() and not self._stop_event.is_set():
                    ib.sleep(0.1)
                    self._process_order_queue(ib, contract)
                    bid = self._positive_decimal(ticker.bid)
                    ask = self._positive_decimal(ticker.ask)
                    last = self._positive_decimal(ticker.last)
                    market_data_type = {
                        1: "Live",
                        2: "Frozen",
                        3: "Delayed",
                        4: "DelayedFrozen",
                    }.get(ticker.marketDataType, f"Unknown({ticker.marketDataType})")
                    current = (bid, ask, last, market_data_type)
                    if current == previous:
                        continue
                    previous = current
                    self._emit_quote(
                        XopQuote(bid, ask, last, datetime.now(SHANGHAI), market_data_type)
                    )
            except Exception as exc:
                terminal_error = str(exc)
                self._emit_status(f"IB连接失败：{exc}", False)
            finally:
                with self._lock:
                    tracked_ib = self._ib
                if tracked_ib is not None:
                    if contract is not None and tracked_ib is ib and ib.isConnected():
                        try:
                            ib.cancelMktData(contract)
                        except Exception:
                            pass
                    try:
                        tracked_ib.client.disconnect()
                    except Exception:
                        pass
                with self._lock:
                    if self._ib is tracked_ib:
                        self._ib = None
                    self.active_client_id = None
                    if self._thread is threading.current_thread():
                        self._thread = None
                self._clear_order_queue("IB连接已结束，未发送的排队订单已清除")
                message = "IB连接失败后已断开" if terminal_error else "IB已断开"
                self._emit_status(message, False)
                if event_loop is not None:
                    event_loop.close()

        thread = threading.Thread(target=run, name="tws-xop", daemon=True)
        with self._lock:
            busy = self._thread is not None and self._thread.is_alive()
            if not busy:
                self._thread = thread
        if busy:
            # Qt slots may synchronously query is_connected(); never emit this
            # signal while holding the same non-reentrant lock.
            self._emit_status("IB已经连接或正在连接", True)
            return
        self._stop_event.clear()
        allocation_mode = "自动分配" if auto_client_id else "手工首选"
        self._emit_status(
            f"正在连接IB {host}:{port}，client ID {candidates[0]}（{allocation_mode}）",
            True,
        )
        thread.start()

    def disconnect_tws(self) -> None:
        self._stop_event.set()
        self._clear_order_queue("IB已断开，未发送的排队订单已清除")
        self._emit_status("IB已断开", False)

    def _clear_order_queue(self, message: str) -> None:
        while True:
            try:
                spec = self._order_queue.get_nowait()
            except queue.Empty:
                break
            self._emit_order_event(
                {"event": "rejected", "order_ref": spec.order_ref, "message": message}
            )

    def _process_order_queue(self, ib, contract) -> None:
        try:
            spec = self._order_queue.get_nowait()
        except queue.Empty:
            return
        try:
            xop_close_orders.validate_future_trigger(spec)
            if spec.order_ref in self._submitted_order_refs:
                raise ValueError("orderRef已存在，禁止重复发送")
            order = xop_close_orders.build_ib_order(spec)
            trade = ib.placeOrder(contract, order)
            self._submitted_order_refs.add(spec.order_ref)
            self._emit_order_event(
                {
                    "event": "submitted",
                    "order_ref": spec.order_ref,
                    "order_id": int(getattr(trade.order, "orderId", 0) or 0),
                    "status": str(getattr(trade.orderStatus, "status", "") or "PendingSubmit"),
                    "message": "已发送到TWS",
                }
            )
        except Exception as exc:
            self._emit_order_event(
                {"event": "rejected", "order_ref": spec.order_ref, "message": str(exc)}
            )

    @staticmethod
    def _positive_decimal(value) -> Decimal | None:
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if number <= 0 or not math.isfinite(number):
            return None
        return Decimal(str(number))
