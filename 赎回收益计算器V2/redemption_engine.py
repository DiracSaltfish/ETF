from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import pandas as pd


TARGET_CODE = "159518"
TARGET_FOREIGN_CODE = "XOP"
DEFAULT_REDEMPTION_UNIT = 1_000_000
DEFAULT_HEDGE_SHARES = 990
DEFAULT_TRANSFER_CONTRACT_GAP = 1000
QMT3_TRANSFER_PRICE_TOLERANCE = Decimal("0.002")
REDEMPTION_SOURCES = frozenset({"QMT1", "QMT2"})
QMT3_SOURCE = "QMT3"
COMPONENT_SHORT_MIN_SYMBOLS = 10
COMPONENT_WINDOW_BEFORE_MINUTES = 5
COMPONENT_WINDOW_AFTER_MINUTES = 20
IB_STATEMENT_TZ = ZoneInfo("America/New_York")
CHINA_TZ = ZoneInfo("Asia/Shanghai")
CHINA_SESSION_START = time(9, 0)
CHINA_SESSION_END = time(15, 0)
BASKET_IB_CUTOFF = time(15, 0)
Q2 = Decimal("0.01")
Q6 = Decimal("0.000001")


def d(value: object) -> Decimal:
    if value is None or value == "" or pd.isna(value):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def money(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_UP)


def hedge_target_from_shares_per_cu(
    redeem_qty: int,
    shares_per_cu: Decimal,
    creation_redemption_unit: int = DEFAULT_REDEMPTION_UNIT,
) -> int:
    if redeem_qty < 0:
        raise ValueError("赎回份额不能为负数")
    if shares_per_cu < 0:
        raise ValueError("单篮子 XOP 股数不能为负数")
    if creation_redemption_unit <= 0:
        raise ValueError("最小申赎单位必须大于 0")
    target = Decimal(redeem_qty) * Decimal(shares_per_cu) / Decimal(creation_redemption_unit)
    return int(target.to_integral_value(rounding=ROUND_CEILING))


def normalize_code(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".0"):
        text = text[:-2]
    return text.split(".", 1)[0]


def trading_day_offset(day: date, count: int, holidays: frozenset[date] = frozenset()) -> date:
    current = day
    step = 1 if count >= 0 else -1
    remaining = abs(count)
    while remaining:
        current = current.fromordinal(current.toordinal() + step)
        if current.weekday() < 5 and current not in holidays:
            remaining -= 1
    return current


@dataclass(frozen=True)
class QmtRecord:
    source: str
    row_number: int
    trade_day: date
    contract_no: int | str
    action: str
    qty: int
    price: Decimal
    amount: Decimal
    code: str
    name: str
    trade_dt: datetime | None = None

    @property
    def key(self) -> tuple[str, int | str, date]:
        return self.source, self.contract_no, self.trade_day


@dataclass(frozen=True)
class LotMatch:
    source: str
    trade_day: date
    contract_no: int | str
    qty: int
    cost: Decimal
    qmt3_hedge_target: int = 0
    qmt3_hedge_open: tuple[IbSlice, ...] = ()


@dataclass
class VenueClose:
    source: str
    trade_day: date
    contract_no: int | str
    qty: int
    proceeds: Decimal
    cost: Decimal
    pnl: Decimal
    matches: tuple[LotMatch, ...]
    inventory_shortfall: int = 0
    trade_dt: datetime | None = None
    hedge_target: int = 0
    ib_open: tuple[IbSlice, ...] = ()
    ib_close: tuple[IbSlice, ...] = ()
    ib_open_shortfall: int = 0
    ib_close_shortfall: int = 0
    ib_trade_pnl_usd: Decimal = Decimal("0")
    ib_pnl_cny: Decimal = Decimal("0")
    qmt3_hedge_target: int = 0
    qmt3_hedge_open: tuple[IbSlice, ...] = ()
    id: str = ""
    manual_ib_mapping: bool = False
    warnings: tuple[str, ...] = ()

    @property
    def ib_open_qty(self) -> int:
        return sum(item.qty for item in self.ib_open)

    @property
    def ib_close_qty(self) -> int:
        return sum(item.qty for item in self.ib_close)

    @property
    def ib_commission_usd(self) -> Decimal:
        return sum(
            (item.commission for item in (*self.ib_open, *self.ib_close)),
            Decimal("0"),
        )

    @property
    def total_pnl_cny(self) -> Decimal:
        return self.pnl + self.ib_pnl_cny

    @property
    def is_complete(self) -> bool:
        return not (
            self.inventory_shortfall
            or self.ib_open_shortfall
            or self.ib_close_shortfall
        )

    @property
    def status(self) -> str:
        if self.inventory_shortfall:
            return "国内库存不足"
        if self.ib_open_shortfall or self.ib_close_shortfall:
            return "IB未完整闭合"
        return "已闭合"


@dataclass(frozen=True)
class IbAllocationClaim:
    """One quantity slice owned by exactly one calculated business outcome."""

    trade_id: str
    side: str
    qty: int
    owner_type: str
    owner_id: str
    leg: str


@dataclass(frozen=True)
class AccountTransfer:
    trade_day: date
    qty: int
    sell_source: str
    sell_contract_no: int | str
    sell_proceeds: Decimal
    sell_fifo_cost: Decimal
    realized_pnl: Decimal
    buy_source: str
    buy_contract_no: int | str
    buy_cost: Decimal
    contract_gap: int | None
    matches: tuple[LotMatch, ...] = ()
    inventory_shortfall: int = 0
    carried_cost: Decimal | None = None
    kind: str = "QMT1/2 调仓"
    qmt3_hedge_target: int = 0
    qmt3_hedge_open: tuple[IbSlice, ...] = ()


@dataclass(frozen=True)
class CashFlow:
    source: str
    trade_day: date
    contract_no: int | str
    action: str
    amount: Decimal
    row_number: int


@dataclass(frozen=True)
class IbTrade:
    id: str
    row_number: int
    dt: datetime
    qty: int
    price: Decimal
    gross: Decimal
    commission: Decimal
    marker: str

    @property
    def side(self) -> str:
        return "SELL" if self.qty < 0 else "BUY"


@dataclass(frozen=True)
class IbStockTrade:
    id: str
    row_number: int
    dt: datetime
    symbol: str
    qty: int
    price: Decimal
    gross: Decimal
    commission: Decimal
    marker: str

    @property
    def side(self) -> str:
        return "SELL" if self.qty < 0 else "BUY"


@dataclass(frozen=True)
class IbSlice:
    trade_id: str
    dt: datetime
    side: str
    qty: int
    price: Decimal
    gross: Decimal
    commission: Decimal
    role: str = ""


@dataclass(frozen=True)
class BorrowFee:
    value_day: date
    qty: int
    amount: Decimal


@dataclass(frozen=True)
class IbSelfClose:
    sequence: int
    opening: IbSlice
    closing: IbSlice
    trade_pnl_usd: Decimal
    fx_rate: Decimal

    @property
    def qty(self) -> int:
        return self.opening.qty

    @property
    def direction(self) -> str:
        return "先卖后买" if self.opening.side == "SELL" else "先买后卖"

    @property
    def commission_usd(self) -> Decimal:
        return self.opening.commission + self.closing.commission

    @property
    def pnl_cny(self) -> Decimal:
        return self.trade_pnl_usd * self.fx_rate


@dataclass
class BasketResult:
    id: str
    sequence: int
    source: str
    redeem_day: date
    contract_no: int | str
    redeem_qty: int
    domestic_cost: Decimal = Decimal("0")
    domestic_matches: tuple[LotMatch, ...] = ()
    inventory_shortfall: int = 0
    refund_amount: Decimal = Decimal("0")
    cash_difference: Decimal = Decimal("0")
    cash_flows: tuple[CashFlow, ...] = ()
    manual_refund_amount: Decimal | None = None
    manual_refund_applied: bool = False
    expected_cash_difference_day: date | None = None
    actual_cash_difference_day: date | None = None
    expected_refund_day: date | None = None
    actual_refund_day: date | None = None
    domestic_pnl: Decimal = Decimal("0")
    hedge_target: int = 0
    ib_open: tuple[IbSlice, ...] = ()
    ib_close: tuple[IbSlice, ...] = ()
    ib_open_shortfall: int = 0
    ib_close_shortfall: int = 0
    ib_trade_pnl_usd: Decimal = Decimal("0")
    ib_borrow_fee_usd: Decimal = Decimal("0")
    ib_pnl_usd: Decimal = Decimal("0")
    fx_rate: Decimal = Decimal("0")
    ib_pnl_cny: Decimal = Decimal("0")
    total_pnl_cny: Decimal = Decimal("0")
    status: str = "待计算"
    warnings: tuple[str, ...] = ()
    manual_ib_mapping: bool = False
    manual_virtual_close: bool = False
    qmt3_hedge_target: int = 0
    qmt3_hedge_open: tuple[IbSlice, ...] = ()
    qmt3_open_overridden: bool = False
    domestic_rollover_target: int = 0
    domestic_rollover_open: tuple[IbSlice, ...] = ()


@dataclass(frozen=True)
class CalculationResult:
    baskets: tuple[BasketResult, ...]
    venue_closes: tuple[VenueClose, ...]
    account_transfers: tuple[AccountTransfer, ...]
    qmt_records: tuple[QmtRecord, ...]
    ib_trades: tuple[IbTrade, ...]
    borrow_fees: tuple[BorrowFee, ...]
    unallocated_ib_sell_qty: int
    unallocated_ib_buy_qty: int
    qmt_latest_day: date | None
    ib_self_closes: tuple[IbSelfClose, ...] = ()
    unmatched_ib: tuple[IbSlice, ...] = ()
    residual_ib_sell_qty: int = 0
    residual_ib_buy_qty: int = 0
    ib_allocations: tuple[IbAllocationClaim, ...] = ()
    allocation_warnings: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def settled_baskets(self) -> tuple[BasketResult, ...]:
        return tuple(item for item in self.baskets if item.status == "已结算")

    @property
    def settled_total_cny(self) -> Decimal:
        return money(sum((item.total_pnl_cny for item in self.settled_baskets), Decimal("0")))

    @property
    def ib_self_close_qty(self) -> int:
        return sum(item.qty for item in self.ib_self_closes)

    @property
    def ib_self_pnl_usd(self) -> Decimal:
        return sum((item.trade_pnl_usd for item in self.ib_self_closes), Decimal("0"))

    @property
    def ib_self_pnl_cny(self) -> Decimal:
        return sum((item.pnl_cny for item in self.ib_self_closes), Decimal("0"))

    @property
    def completed_strategy_self_closes(self) -> tuple[VenueClose, ...]:
        return tuple(item for item in self.venue_closes if item.is_complete)

    @property
    def strategy_self_close_qty(self) -> int:
        return sum(item.hedge_target for item in self.completed_strategy_self_closes)

    @property
    def strategy_self_domestic_pnl_cny(self) -> Decimal:
        return sum(
            (item.pnl for item in self.completed_strategy_self_closes),
            Decimal("0"),
        )

    @property
    def strategy_self_ib_pnl_usd(self) -> Decimal:
        return sum(
            (item.ib_trade_pnl_usd for item in self.completed_strategy_self_closes),
            Decimal("0"),
        )

    @property
    def strategy_self_ib_pnl_cny(self) -> Decimal:
        return sum(
            (item.ib_pnl_cny for item in self.completed_strategy_self_closes),
            Decimal("0"),
        )

    @property
    def strategy_self_total_cny(self) -> Decimal:
        return sum(
            (item.total_pnl_cny for item in self.completed_strategy_self_closes),
            Decimal("0"),
        )

    @property
    def incomplete_strategy_self_close_count(self) -> int:
        return sum(not item.is_complete for item in self.venue_closes)


@dataclass
class _InventoryLot:
    source: str
    trade_day: date
    contract_no: int | str
    qty: int
    cost: Decimal
    qmt3_hedge_target: int = 0
    qmt3_hedge_open: list[IbSlice] = field(default_factory=list)


@dataclass(frozen=True)
class Qmt3OpenHedge:
    """IB short-open provenance attached to one original QMT3 buy."""

    target_qty: int
    slices: tuple[IbSlice, ...]


@dataclass(frozen=True)
class DomesticRolloverLink:
    """Virtual hedge-open provenance attached to a domestic replacement buy."""

    close_basket_id: str
    source: str
    trade_day: date
    contract_no: int | str
    domestic_qty: int
    open_slice: IbSlice


def _required_columns(frame: pd.DataFrame, path: Path, source: str) -> dict[str, str]:
    aliases = {
        "code": ("证券代码",),
        "action": ("操作",),
        "qty": ("成交数量",),
        "amount": ("发生金额",),
        "contract": ("合同编号",),
        "trade_day": ("成交日期", "交收日期") if source == QMT3_SOURCE else ("交收日期", "成交日期"),
        "price": ("成交均价",),
        "name": ("证券名称", "证券中文全称"),
        "trade_time": ("成交时间",),
    }
    columns = set(frame.columns)
    mapping = {
        field: next((column for column in candidates if column in columns), "")
        for field, candidates in aliases.items()
    }
    required = ("code", "action", "qty", "amount", "contract", "trade_day")
    missing = [aliases[field][0] for field in required if not mapping[field]]
    if missing:
        raise ValueError(f"{path.name} 缺少字段: {', '.join(missing)}")
    return mapping


def _row_value(row: pd.Series, columns: dict[str, str], field: str) -> object:
    column = columns.get(field) or ""
    return row.get(column) if column else None


def _normalize_qmt_action(action: object, source: str) -> str | None:
    if action is None or pd.isna(action):
        return None
    text = str(action).strip()
    if not text:
        return None
    if source != QMT3_SOURCE:
        return text
    if text == "买入":
        return "证券买入"
    if text == "卖出":
        return "证券卖出"
    # QMT3 is a non-redeeming account. Its ETF redemption and cash rows must
    # never enter the basket/cash-flow path even if a future export includes them.
    return None


def _contract_text(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    text = str(value).strip()
    return text[:-2] if text.endswith(".0") else text


def contract_sort_key(contract_no: int | str) -> tuple[int, int, str]:
    text = _contract_text(contract_no)
    if text.isdigit():
        return 0, int(text), text
    return 1, 0, text


def _contract_distance(left: int | str, right: int | str) -> int | None:
    left_text = _contract_text(left)
    right_text = _contract_text(right)
    if not left_text.isdigit() or not right_text.isdigit():
        return None
    return abs(int(left_text) - int(right_text))


def load_qmt_file(path: Path | str | None, source: str) -> list[QmtRecord]:
    if path is None or not str(path).strip():
        return []
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"{source} 文件不存在: {file_path}")
    frame = pd.read_excel(file_path)
    columns = _required_columns(frame, file_path, source)
    records: list[QmtRecord] = []
    for row_number, row in frame.iterrows():
        if normalize_code(_row_value(row, columns, "code")) != TARGET_CODE:
            continue
        action = _normalize_qmt_action(_row_value(row, columns, "action"), source)
        if action is None:
            continue
        try:
            trade_day = _parse_yyyymmdd(_row_value(row, columns, "trade_day"))
            qty = int(d(_row_value(row, columns, "qty")))
        except (ValueError, TypeError):
            continue
        if trade_day is None:
            continue
        contract_value = _row_value(row, columns, "contract")
        contract_no: int | str
        if source == QMT3_SOURCE:
            contract_no = _contract_text(contract_value)
            if not contract_no:
                continue
        else:
            contract_no = int(d(contract_value))
        records.append(
            QmtRecord(
                source=source,
                row_number=int(row_number) + 2,
                trade_day=trade_day,
                contract_no=contract_no,
                action=action,
                qty=qty,
                price=d(_row_value(row, columns, "price")),
                amount=d(_row_value(row, columns, "amount")),
                code=TARGET_CODE,
                name=str(_row_value(row, columns, "name") or "").strip(),
                trade_dt=_parse_qmt_timestamp(
                    _row_value(row, columns, "trade_day"),
                    _row_value(row, columns, "trade_time"),
                ) if source == QMT3_SOURCE else None,
            )
        )
    return records


def _parse_yyyymmdd(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        pass
    try:
        return datetime.strptime(str(int(d(text))), "%Y%m%d").date()
    except (ValueError, TypeError):
        return None


def _parse_qmt_timestamp(day_value: object, time_value: object, *iso_values: object) -> datetime | None:
    for value in iso_values:
        text = str(value or "").strip()
        if not text:
            continue
        try:
            return datetime.fromisoformat(text)
        except ValueError:
            continue
    day = _parse_yyyymmdd(day_value)
    if day is None or time_value is None or pd.isna(time_value):
        return None
    if isinstance(time_value, time):
        return datetime.combine(day, time_value)
    text = str(time_value).strip()
    for pattern in ("%H:%M:%S", "%H:%M", "%H%M%S"):
        try:
            return datetime.combine(day, datetime.strptime(text.split(".", 1)[0].zfill(6), pattern).time())
        except ValueError:
            continue
    return None


def _qmt_contract_candidates(contract_no: int | str) -> tuple[str, ...]:
    text = _contract_text(contract_no)
    values = [text]
    if text.isdigit():
        numeric_value = int(text)
        if numeric_value >= 3_800_000_000:
            values.append(str(numeric_value - 3_800_000_000))
        values.append(str(numeric_value % 1_000_000))
    return tuple(dict.fromkeys(values))


def _qmt_direction(action: str) -> str:
    if "买入" in action:
        return "买入"
    if "卖出" in action or "赎回" in action:
        return "卖出"
    return action


def _qmt_time_hint_files(root: Path, trade_days: set[date]) -> list[Path]:
    candidates: list[Path] = []
    day_dirs = [root] if (root / "QMT成交时间.csv").exists() else []
    day_dirs.extend(root / f"{day:%Y%m%d}" for day in sorted(trade_days))
    for day_dir in day_dirs:
        for name in ("QMT成交时间.csv", "QMT1.csv", "QMT2.csv"):
            path = day_dir / name
            if path.exists():
                candidates.append(path)
    return candidates


def _load_qmt_time_hints(
    root: Path | str | None,
    trade_days: set[date],
) -> dict[tuple[str, date, str, str, int], datetime]:
    if root is None or not str(root).strip():
        return {}
    root_path = Path(root).expanduser()
    if not root_path.exists():
        return {}
    hints: dict[tuple[str, date, str, str, int], datetime] = {}
    for path in _qmt_time_hint_files(root_path, trade_days):
        try:
            handle = path.open("r", encoding="utf-8-sig", newline="")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                code = normalize_code(row.get("代码") or row.get("证券代码") or row.get("证券代码(原始)"))
                if code != TARGET_CODE:
                    continue
                source = str(row.get("QMT窗口") or row.get("窗口") or row.get("来源") or path.stem).strip().upper()
                if source not in REDEMPTION_SOURCES:
                    continue
                day = _parse_yyyymmdd(row.get("交易日") or row.get("处理日期") or row.get("委托日期"))
                if day is None or (trade_days and day not in trade_days):
                    continue
                order_no = str(row.get("委托号") or "").strip()
                if not order_no:
                    continue
                direction = str(row.get("方向") or row.get("买卖方向") or "").strip()
                if direction not in {"买入", "卖出"}:
                    continue
                qty = int(d(row.get("成交数量")))
                if qty <= 0:
                    continue
                if row.get("委托状态") and "已成交" not in str(row.get("委托状态")):
                    continue
                event_type = str(row.get("事件类型") or "")
                if event_type and "成交" not in event_type:
                    continue
                trade_dt = _parse_qmt_timestamp(
                    row.get("处理日期") or row.get("交易日") or row.get("委托日期"),
                    row.get("委托时间"),
                    row.get("成交处理时间"),
                    row.get("记录时间"),
                )
                if trade_dt is None:
                    continue
                hints.setdefault((source, day, order_no, direction, qty), trade_dt)
    return hints


def _enrich_qmt_record_time(
    record: QmtRecord,
    hints: dict[tuple[str, date, str, str, int], datetime],
) -> QmtRecord:
    direction = _qmt_direction(record.action)
    for order_no in _qmt_contract_candidates(record.contract_no):
        hint = hints.get((record.source, record.trade_day, order_no, direction, record.qty))
        if hint is not None:
            return replace(record, trade_dt=hint)
    return record


def load_qmt_records(
    qmt_paths: dict[str, Path | str | None],
    qmt_time_root: Path | str | None = None,
) -> list[QmtRecord]:
    records: list[QmtRecord] = []
    for source in sorted(qmt_paths):
        records.extend(load_qmt_file(qmt_paths[source], source))
    hints = _load_qmt_time_hints(qmt_time_root, {item.trade_day for item in records})
    if hints:
        records = [_enrich_qmt_record_time(item, hints) for item in records]
    # QMT1/QMT2 share the redeemable virtual inventory; QMT3 is accounted for
    # separately later so its holdings cannot be consumed by a redemption.
    records.sort(key=lambda item: (item.trade_day, contract_sort_key(item.contract_no), item.source, item.row_number))
    return records


def _trade_id(raw: list[str], occurrence: int) -> str:
    payload = "|".join(raw + [str(occurrence)])
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:20]


def load_ib_statement(path: Path | str) -> tuple[list[IbTrade], list[BorrowFee]]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"IB 文件不存在: {file_path}")
    trades: list[IbTrade] = []
    fees: list[BorrowFee] = []
    signatures: dict[tuple[str, ...], int] = {}
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, raw in enumerate(csv.reader(handle), start=1):
            if len(raw) >= 16 and raw[0] == "交易" and raw[1] == "Data" and raw[5] == TARGET_FOREIGN_CODE:
                signature = tuple(raw[:16])
                occurrence = signatures.get(signature, 0) + 1
                signatures[signature] = occurrence
                try:
                    trade_dt = datetime.strptime(raw[6].strip(), "%Y-%m-%d, %H:%M:%S")
                    qty = int(d(raw[7]))
                    price = d(raw[8])
                    gross = abs(d(raw[10]))
                    commission = abs(d(raw[11]))
                except (ValueError, TypeError):
                    continue
                if qty == 0:
                    continue
                trades.append(
                    IbTrade(
                        id=_trade_id(list(signature), occurrence),
                        row_number=row_number,
                        dt=trade_dt,
                        qty=qty,
                        price=price,
                        gross=gross,
                        commission=commission,
                        marker=raw[15].strip(),
                    )
                )
                continue
            if len(raw) >= 11 and raw[0] == "借入费用详情" and raw[1] == "Data" and raw[4] == TARGET_FOREIGN_CODE:
                try:
                    value_day = datetime.strptime(raw[3].strip(), "%Y-%m-%d").date()
                    qty = int(abs(d(raw[5])))
                    amount = abs(d(raw[9]))
                except (ValueError, TypeError):
                    continue
                fees.append(BorrowFee(value_day=value_day, qty=qty, amount=amount))
    trades.sort(key=lambda item: (item.dt, item.row_number, item.id))
    fees.sort(key=lambda item: item.value_day)
    return trades, fees


def load_ib_stock_trades(path: Path | str) -> list[IbStockTrade]:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"IB 文件不存在: {file_path}")
    trades: list[IbStockTrade] = []
    signatures: dict[tuple[str, ...], int] = {}
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, raw in enumerate(csv.reader(handle), start=1):
            if len(raw) < 16 or raw[0] != "交易" or raw[1] != "Data" or raw[3] != "股票":
                continue
            symbol = raw[5].strip().upper()
            if not symbol:
                continue
            signature = tuple(raw[:16])
            occurrence = signatures.get(signature, 0) + 1
            signatures[signature] = occurrence
            try:
                trade_dt = datetime.strptime(raw[6].strip(), "%Y-%m-%d, %H:%M:%S")
                qty = int(d(raw[7]))
                price = d(raw[8])
                gross = abs(d(raw[10]))
                commission = abs(d(raw[11]))
            except (ValueError, TypeError):
                continue
            if qty == 0:
                continue
            trades.append(
                IbStockTrade(
                    id=_trade_id(list(signature), occurrence),
                    row_number=row_number,
                    dt=trade_dt,
                    symbol=symbol,
                    qty=qty,
                    price=price,
                    gross=gross,
                    commission=commission,
                    marker=raw[15].strip(),
                )
            )
    trades.sort(key=lambda item: (item.dt, item.row_number, item.id))
    return trades


def _component_short_windows(stock_trades: list[IbStockTrade]) -> tuple[tuple[datetime, datetime], ...]:
    by_day: dict[date, list[IbStockTrade]] = {}
    for trade in stock_trades:
        if trade.symbol == TARGET_FOREIGN_CODE or trade.qty >= 0:
            continue
        by_day.setdefault(trade.dt.date(), []).append(trade)
    windows: list[tuple[datetime, datetime]] = []
    for day_trades in by_day.values():
        symbols = {item.symbol for item in day_trades}
        if len(symbols) < COMPONENT_SHORT_MIN_SYMBOLS:
            continue
        start = min(item.dt for item in day_trades) - timedelta(minutes=COMPONENT_WINDOW_BEFORE_MINUTES)
        end = max(item.dt for item in day_trades) + timedelta(minutes=COMPONENT_WINDOW_AFTER_MINUTES)
        windows.append((start, end))
    return tuple(sorted(windows))


def _in_component_window(moment: datetime, windows: tuple[tuple[datetime, datetime], ...]) -> bool:
    return any(start <= moment <= end for start, end in windows)


def _derive_ib_trade(raw: IbTrade, qty: int, role: str, index: int) -> IbTrade:
    ratio = Decimal(qty) / Decimal(abs(raw.qty))
    signed_qty = -qty if raw.qty < 0 else qty
    marker = f"{raw.marker};AUTO_{role.upper()}" if raw.marker else f"AUTO_{role.upper()}"
    return IbTrade(
        id=f"{raw.id}:{role}:{index}",
        row_number=raw.row_number,
        dt=raw.dt,
        qty=signed_qty,
        price=raw.price,
        gross=raw.gross * ratio,
        commission=raw.commission * ratio,
        marker=marker,
    )


def _synthetic_ib_trade(
    *,
    trade_id: str,
    row_number: int,
    dt: datetime,
    qty: int,
    gross: Decimal,
    role: str,
) -> IbTrade:
    price = gross / Decimal(abs(qty)) if qty else Decimal("0")
    return IbTrade(
        id=trade_id,
        row_number=row_number,
        dt=dt,
        qty=qty,
        price=price,
        gross=gross,
        commission=Decimal("0"),
        marker=f"AUTO_{role.upper()}",
    )


def build_ib_hedge_trades(
    xop_trades: list[IbTrade],
    stock_trades: list[IbStockTrade],
    *,
    include_unmatched_buys: bool = False,
) -> list[IbTrade]:
    """Derive the XOP-equivalent hedge stream used by basket matching.

    Component-stock short windows mark nearby XOP buys as base inventory rather
    than ordinary redemption closes. Selling that base inventory later becomes
    an XOP-equivalent synthetic short open priced at the XOP sale.
    """
    windows = _component_short_windows(stock_trades)
    derived: list[IbTrade] = []
    direct_short_qty = 0
    xop_base_qty = 0
    component_equivalent_qty = 0
    sequence = 0
    for trade in xop_trades:
        remaining = abs(trade.qty)
        in_component_window = _in_component_window(trade.dt, windows)
        if trade.qty < 0:
            if xop_base_qty:
                used = min(xop_base_qty, remaining)
                if used:
                    sequence += 1
                    derived.append(_derive_ib_trade(trade, used, "synthetic_open", sequence))
                    xop_base_qty -= used
                    remaining -= used
            if remaining:
                sequence += 1
                derived.append(_derive_ib_trade(trade, remaining, "direct_open", sequence))
                direct_short_qty += remaining
            continue

        if in_component_window:
            component_equivalent_qty += remaining
        if direct_short_qty:
            used = min(direct_short_qty, remaining)
            if used:
                sequence += 1
                derived.append(_derive_ib_trade(trade, used, "direct_close", sequence))
                direct_short_qty -= used
                remaining -= used
        if remaining and in_component_window:
            component_gap = max(0, component_equivalent_qty - xop_base_qty)
            used = min(component_gap if component_gap else remaining, remaining)
            xop_base_qty += used
            remaining -= used
        if remaining and include_unmatched_buys:
            sequence += 1
            derived.append(_derive_ib_trade(trade, remaining, "direct_close", sequence))
    derived.sort(key=lambda item: (item.dt, item.row_number, item.id))
    return derived


def _used_domestic_buy_qty(
    baskets: list[BasketResult],
    venue_closes: list[VenueClose],
    account_transfers: list[AccountTransfer],
) -> dict[tuple[str, date, int | str], int]:
    used: dict[tuple[str, date, int | str], int] = {}

    def add(match: LotMatch) -> None:
        key = (match.source, match.trade_day, match.contract_no)
        used[key] = used.get(key, 0) + match.qty

    for close in venue_closes:
        for match in close.matches:
            add(match)
    for transfer in account_transfers:
        for match in transfer.matches:
            add(match)
    return used


def _next_weekday(day: date) -> date:
    return trading_day_offset(day, 1)


def _domestic_rollover_buy_candidates(
    records: list[QmtRecord],
    used_buy_qty: dict[tuple[str, date, int | str], int],
    day: date,
) -> list[tuple[QmtRecord, int, int]]:
    candidates: list[tuple[QmtRecord, int, int]] = []
    for record in records:
        if (
            record.source not in REDEMPTION_SOURCES
            or record.trade_day != day
            or record.action != "证券买入"
            or record.qty <= 0
        ):
            continue
        available_qty = record.qty - used_buy_qty.get((record.source, record.trade_day, record.contract_no), 0)
        if available_qty <= 0:
            continue
        if record.trade_dt is not None and not (CHINA_SESSION_START <= record.trade_dt.time() <= CHINA_SESSION_END):
            continue
        hedge_qty = hedge_target_from_shares_per_cu(available_qty, Decimal(DEFAULT_HEDGE_SHARES))
        if hedge_qty <= 0:
            continue
        candidates.append((record, available_qty, hedge_qty))
    candidates.sort(
        key=lambda item: (
            item[0].trade_dt or datetime.combine(item[0].trade_day, time.max),
            contract_sort_key(item[0].contract_no),
            item[0].source,
            item[0].row_number,
        )
    )
    return candidates


def _build_domestic_rollover_plan(
    baskets: list[BasketResult],
    venue_closes: list[VenueClose],
    account_transfers: list[AccountTransfer],
    records: list[QmtRecord],
    fx_rate: Decimal,
    overrides: dict[str, dict[str, object]] | None = None,
) -> tuple[list[IbTrade], list[DomesticRolloverLink]]:
    """Create XOP-equivalent rollover trades from next-day domestic buys.

    If a redemption basket has no meaningful XOP BUY close, but the next China
    session has unused 159518 buys for roughly one basket, those buys mark the
    old hedge to market and immediately reopen the hedge for the new domestic
    inventory.
    """
    if fx_rate <= 0:
        return [], []
    used_buy_qty = _used_domestic_buy_qty(baskets, venue_closes, account_transfers)
    used_rollover_keys: dict[tuple[str, date, int | str], int] = {}
    synthetic: list[IbTrade] = []
    links: list[DomesticRolloverLink] = []
    sequence = 0
    for basket in sorted(baskets, key=lambda item: item.sequence):
        force_virtual_close = _manual_virtual_close((overrides or {}).get(basket.id))
        basket.manual_virtual_close = force_virtual_close
        close_qty = sum(item.qty for item in basket.ib_close)
        if basket.hedge_target <= 0:
            continue
        if (
            not force_virtual_close
            and basket.ib_close_shortfall < (basket.hedge_target // 2)
            and close_qty >= (basket.hedge_target // 2)
        ):
            continue
        replacement_day = _next_weekday(basket.redeem_day)
        remaining = basket.hedge_target
        candidates = _domestic_rollover_buy_candidates(records, used_buy_qty, replacement_day)
        selected: list[tuple[QmtRecord, int, int, int]] = []
        for record, available_qty, hedge_qty in candidates:
            key = (record.source, record.trade_day, record.contract_no)
            already_used = used_rollover_keys.get(key, 0)
            available_hedge_qty = max(0, hedge_qty - already_used)
            if available_hedge_qty <= 0:
                continue
            used_hedge_qty = min(available_hedge_qty, remaining)
            if used_hedge_qty <= 0:
                continue
            selected.append((record, available_qty, used_hedge_qty, hedge_qty))
            used_rollover_keys[key] = already_used + used_hedge_qty
            remaining -= used_hedge_qty
            if remaining <= 0:
                break
        if remaining > max(1, basket.hedge_target // 20):
            for record, _available_qty, used_hedge_qty, _hedge_qty in selected:
                key = (record.source, record.trade_day, record.contract_no)
                used_rollover_keys[key] = max(0, used_rollover_keys.get(key, 0) - used_hedge_qty)
            continue
        for record, available_qty, used_hedge_qty, hedge_qty in selected:
            sequence += 1
            domestic_qty = min(
                available_qty,
                int(
                    (
                        Decimal(available_qty)
                        * Decimal(used_hedge_qty)
                        / Decimal(hedge_qty)
                    ).to_integral_value(rounding=ROUND_CEILING)
                ),
            )
            gross_usd = (
                abs(record.amount)
                * Decimal(domestic_qty)
                / Decimal(record.qty)
                / fx_rate
            )
            china_dt = record.trade_dt or datetime.combine(record.trade_day, CHINA_SESSION_START)
            ib_dt = china_dt_to_ib_statement_dt(china_dt)
            base_id = f"domestic-rollover:{basket.id}:{record.source}:{record.trade_day:%Y%m%d}:{record.contract_no}:{sequence}"
            close_trade = _synthetic_ib_trade(
                trade_id=f"{base_id}:domestic_rollover_close",
                row_number=10_000_000 + sequence * 2 - 1,
                dt=ib_dt,
                qty=used_hedge_qty,
                gross=gross_usd,
                role="domestic_rollover_close",
            )
            open_trade = _synthetic_ib_trade(
                trade_id=f"{base_id}:domestic_rollover_open",
                row_number=10_000_000 + sequence * 2,
                dt=ib_dt,
                qty=-used_hedge_qty,
                gross=gross_usd,
                role="domestic_rollover_open",
            )
            synthetic.extend((close_trade, open_trade))
            links.append(
                DomesticRolloverLink(
                    close_basket_id=basket.id,
                    source=record.source,
                    trade_day=record.trade_day,
                    contract_no=record.contract_no,
                    domestic_qty=domestic_qty,
                    open_slice=IbSlice(
                        trade_id=open_trade.id,
                        dt=open_trade.dt,
                        side=open_trade.side,
                        qty=abs(open_trade.qty),
                        price=open_trade.price,
                        gross=open_trade.gross,
                        commission=open_trade.commission,
                        role="domestic_rollover_open",
                    ),
                )
            )
    synthetic.sort(key=lambda item: (item.dt, item.row_number, item.id))
    return synthetic, links


def build_domestic_rollover_trades(
    baskets: list[BasketResult],
    venue_closes: list[VenueClose],
    account_transfers: list[AccountTransfer],
    records: list[QmtRecord],
    fx_rate: Decimal,
    overrides: dict[str, dict[str, object]] | None = None,
) -> list[IbTrade]:
    trades, _links = _build_domestic_rollover_plan(
        baskets,
        venue_closes,
        account_transfers,
        records,
        fx_rate,
        overrides,
    )
    return trades


def attach_domestic_rollover_opens(
    baskets: list[BasketResult],
    links: list[DomesticRolloverLink],
) -> None:
    """Carry each virtual open with the domestic FIFO lot that created it."""
    lots_by_key: dict[tuple[str, date, int | str], list[list[object]]] = {}
    for link in links:
        key = (link.source, link.trade_day, link.contract_no)
        lots_by_key.setdefault(key, []).append(
            [link.domestic_qty, [link.open_slice]]
        )

    for basket in sorted(baskets, key=lambda item: item.sequence):
        carried: list[IbSlice] = []
        for match in basket.domestic_matches:
            key = (match.source, match.trade_day, match.contract_no)
            match_remaining = match.qty
            for lot in lots_by_key.get(key, []):
                if match_remaining <= 0:
                    break
                domestic_remaining = int(lot[0])
                slices = lot[1]
                if domestic_remaining <= 0 or not slices:
                    continue
                used_domestic = min(domestic_remaining, match_remaining)
                hedge_remaining = sum(item.qty for item in slices)
                hedge_qty = min(
                    hedge_remaining,
                    hedge_target_from_shares_per_cu(
                        used_domestic,
                        Decimal(hedge_remaining),
                        domestic_remaining,
                    ),
                )
                used_slices, _shortfall = _consume_carried_ib_slices(slices, hedge_qty)
                carried.extend(used_slices)
                lot[0] = domestic_remaining - used_domestic
                match_remaining -= used_domestic
        basket.domestic_rollover_open = tuple(carried)
        basket.domestic_rollover_target = sum(item.qty for item in carried)


def _consume_carried_ib_slices(
    slices: list[IbSlice],
    target_qty: int,
) -> tuple[tuple[IbSlice, ...], int]:
    remaining = max(0, target_qty)
    consumed: list[IbSlice] = []
    while remaining > 0 and slices:
        item = slices[0]
        used = min(item.qty, remaining)
        ratio = Decimal(used) / Decimal(item.qty)
        consumed.append(
            replace(
                item,
                qty=used,
                gross=item.gross * ratio,
                commission=item.commission * ratio,
            )
        )
        remaining -= used
        if used == item.qty:
            slices.pop(0)
        else:
            slices[0] = replace(
                item,
                qty=item.qty - used,
                gross=item.gross * (Decimal("1") - ratio),
                commission=item.commission * (Decimal("1") - ratio),
            )
    return tuple(consumed), remaining


def _consume_inventory(lots: list[_InventoryLot], target_qty: int) -> tuple[Decimal, tuple[LotMatch, ...], int]:
    remaining = max(0, target_qty)
    cost = Decimal("0")
    matches: list[LotMatch] = []
    while remaining > 0 and lots:
        lot = lots[0]
        used = min(lot.qty, remaining)
        used_cost = lot.cost * Decimal(used) / Decimal(lot.qty)
        hedge_target = min(
            lot.qmt3_hedge_target,
            hedge_target_from_shares_per_cu(
                used,
                Decimal(lot.qmt3_hedge_target),
                lot.qty,
            ),
        )
        hedge_open, _hedge_shortfall = _consume_carried_ib_slices(lot.qmt3_hedge_open, hedge_target)
        matches.append(
            LotMatch(
                source=lot.source,
                trade_day=lot.trade_day,
                contract_no=lot.contract_no,
                qty=used,
                cost=used_cost,
                qmt3_hedge_target=hedge_target,
                qmt3_hedge_open=hedge_open,
            )
        )
        cost += used_cost
        lot.qty -= used
        lot.cost -= used_cost
        lot.qmt3_hedge_target -= hedge_target
        remaining -= used
        if lot.qty == 0:
            lots.pop(0)
    return cost, tuple(matches), remaining


def _basket_id(record: QmtRecord) -> str:
    raw = f"{record.source}|{record.trade_day:%Y%m%d}|{record.contract_no}|{record.qty}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _venue_close_id(record: QmtRecord) -> str:
    raw = (
        f"{record.source}|{record.trade_day:%Y%m%d}|{record.contract_no}|"
        f"{record.qty}|{record.amount}|{record.row_number}"
    )
    return f"venue:{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]}"


def _qmt_record_id(record: QmtRecord) -> tuple[str, int]:
    return record.source, record.row_number


def _record_sort_key(record: QmtRecord) -> tuple[object, ...]:
    return (
        record.trade_day,
        record.trade_dt or datetime.combine(record.trade_day, time.max),
        contract_sort_key(record.contract_no),
        record.source,
        record.row_number,
    )


def identify_account_transfers(
    records: list[QmtRecord],
    max_contract_gap: int = DEFAULT_TRANSFER_CONTRACT_GAP,
) -> dict[tuple[str, int], QmtRecord]:
    """Identify conservative cross-account opposite-side transfer pairs.

    Full QMT statements have no execution time. Contract order is therefore the
    only available intraday proxy, and only exact-quantity pairs are accepted.
    """
    trades = [
        item
        for item in records
        if item.source in REDEMPTION_SOURCES and item.action in {"证券买入", "证券卖出"} and item.qty > 0
    ]
    buys = [item for item in trades if item.action == "证券买入"]
    used_buys: set[tuple[str, int]] = set()
    used_sells: set[tuple[str, int]] = set()
    matches: dict[tuple[str, int], QmtRecord] = {}
    for sell in (item for item in trades if item.action == "证券卖出"):
        candidates = [
            buy
            for buy in buys
            if _qmt_record_id(buy) not in used_buys
            and buy.trade_day == sell.trade_day
            and buy.source != sell.source
            and buy.qty == sell.qty
            and (_contract_distance(buy.contract_no, sell.contract_no) or 0) <= max_contract_gap
        ]
        if not candidates:
            continue
        buy = min(
            candidates,
            key=lambda item: (_contract_distance(item.contract_no, sell.contract_no) or 0, item.source, item.row_number),
        )
        matches[_qmt_record_id(sell)] = buy
        used_buys.add(_qmt_record_id(buy))
        used_sells.add(_qmt_record_id(sell))
    unmatched_sells = [
        item for item in trades
        if item.action == "证券卖出" and _qmt_record_id(item) not in used_sells
    ]
    unmatched_buys = [
        item for item in buys
        if _qmt_record_id(item) not in used_buys
    ]
    group_keys = sorted({
        (sell.trade_day, sell.source, buy.source)
        for sell in unmatched_sells
        for buy in unmatched_buys
        if sell.trade_day == buy.trade_day and sell.source != buy.source
    })
    for day, sell_source, buy_source in group_keys:
        sells = [
            item for item in unmatched_sells
            if item.trade_day == day and item.source == sell_source and _qmt_record_id(item) not in used_sells
        ]
        candidate_buys = [
            item for item in unmatched_buys
            if item.trade_day == day and item.source == buy_source and _qmt_record_id(item) not in used_buys
        ]
        if not sells or not candidate_buys:
            continue
        if len(sells) == 1 and len(candidate_buys) == 1:
            continue
        if sum(item.qty for item in sells) != sum(item.qty for item in candidate_buys):
            continue
        for sell in sorted(sells, key=_record_sort_key):
            candidates = [
                buy for buy in candidate_buys
                if _qmt_record_id(buy) not in used_buys and buy.qty == sell.qty
            ]
            if not candidates:
                continue
            buy = min(
                candidates,
                key=lambda item: (
                    _contract_distance(item.contract_no, sell.contract_no) or 0,
                    contract_sort_key(item.contract_no),
                    item.row_number,
                ),
            )
            matches[_qmt_record_id(sell)] = buy
            used_buys.add(_qmt_record_id(buy))
            used_sells.add(_qmt_record_id(sell))
    return matches


def identify_qmt3_transfer_receivers(
    records: list[QmtRecord],
    occupied_buy_ids: set[tuple[str, int]] = frozenset(),
) -> dict[tuple[str, int], QmtRecord]:
    """Match only conservative QMT3-to-redeeming-account inventory transfers.

    QMT3 has a different statement format and cannot redeem. Without shared
    contract numbering, only same-day, exact-quantity, near-identical-price
    QMT3 sell -> QMT1/QMT2 buy pairs are treated as transfers.
    """
    qmt3_sells = sorted(
        [
            item
            for item in records
            if item.source == QMT3_SOURCE and item.action == "证券卖出" and item.qty > 0
        ],
        key=_record_sort_key,
    )
    receiving_buys = [
        item
        for item in records
        if item.source in REDEMPTION_SOURCES
        and item.action == "证券买入"
        and item.qty > 0
        and _qmt_record_id(item) not in occupied_buy_ids
    ]
    used_buy_ids: set[tuple[str, int]] = set()
    receivers: dict[tuple[str, int], QmtRecord] = {}
    for sell in qmt3_sells:
        candidates = [
            buy
            for buy in receiving_buys
            if _qmt_record_id(buy) not in used_buy_ids
            and buy.trade_day == sell.trade_day
            and buy.qty == sell.qty
            and abs(buy.price - sell.price) <= QMT3_TRANSFER_PRICE_TOLERANCE
        ]
        if not candidates:
            continue
        buy = min(
            candidates,
            key=lambda item: (abs(item.price - sell.price), item.source, contract_sort_key(item.contract_no), item.row_number),
        )
        receivers[_qmt_record_id(buy)] = sell
        used_buy_ids.add(_qmt_record_id(buy))
    return receivers


def _base_ib_trade_id(trade_id: str) -> str:
    return trade_id.split(":", 1)[0]


def _qmt_open_order_key(record: QmtRecord) -> tuple[object, ...]:
    moment = record.trade_dt or datetime.combine(record.trade_day, time.max)
    minute = moment.replace(second=0, microsecond=0)
    return (
        record.trade_day,
        minute,
        contract_sort_key(record.contract_no),
        record.source,
        record.row_number,
    )


def build_qmt3_open_hedges(
    records: list[QmtRecord],
    trades: list[IbTrade],
    transfer_contract_gap: int = DEFAULT_TRANSFER_CONTRACT_GAP,
) -> tuple[
    dict[tuple[str, int], Qmt3OpenHedge],
    dict[str, int],
    tuple[str, ...],
]:
    """Link same-day China-session XOP SELLs to genuine domestic opens.

    QMT1/QMT2 are one virtual system and QMT3 is another.  Internal transfer
    receiver buys are excluded so they cannot create a second foreign open.
    Within the same minute, QMT records follow contract number and IB fills
    follow the activity-statement row order.
    """
    transfer_matches = identify_account_transfers(
        [item for item in records if item.source in REDEMPTION_SOURCES],
        transfer_contract_gap,
    )
    occupied_buy_ids = {_qmt_record_id(item) for item in transfer_matches.values()}
    qmt3_receivers = identify_qmt3_transfer_receivers(records, occupied_buy_ids)
    receiver_ids = occupied_buy_ids | set(qmt3_receivers)
    opening_buys = sorted(
        (
            item
            for item in records
            if item.source in REDEMPTION_SOURCES | {QMT3_SOURCE}
            and item.action == "证券买入"
            and item.qty > 0
            and _qmt_record_id(item) not in receiver_ids
        ),
        key=_qmt_open_order_key,
    )

    running_shares: dict[tuple[date, str], int] = {}
    running_targets: dict[tuple[date, str], int] = {}
    demands: list[tuple[QmtRecord, int]] = []
    for record in opening_buys:
        system = QMT3_SOURCE if record.source == QMT3_SOURCE else "QMT1/2"
        key = (record.trade_day, system)
        new_shares = running_shares.get(key, 0) + record.qty
        new_target = hedge_target_from_shares_per_cu(new_shares, Decimal(DEFAULT_HEDGE_SHARES))
        target = new_target - running_targets.get(key, 0)
        running_shares[key] = new_shares
        running_targets[key] = new_target
        demands.append((record, target))

    supplies_by_day: dict[date, list[list[object]]] = {}
    for trade in trades:
        if trade.qty >= 0 or _trade_role(trade) not in {"direct_open", "synthetic_open"}:
            continue
        china_dt = ib_statement_to_china_dt(trade.dt)
        if not (CHINA_SESSION_START <= china_dt.time() <= CHINA_SESSION_END):
            continue
        supplies_by_day.setdefault(china_dt.date(), []).append([trade, abs(trade.qty)])
    for supplies in supplies_by_day.values():
        supplies.sort(
            key=lambda item: (
                ib_statement_to_china_dt(item[0].dt).replace(second=0, microsecond=0),
                item[0].row_number,
                item[0].id,
            )
        )

    hedges: dict[tuple[str, int], Qmt3OpenHedge] = {}
    reserved: dict[str, int] = {}
    qmt3_totals: dict[date, list[int]] = {}
    for record, target in demands:
        remaining = target
        linked: list[IbSlice] = []
        supplies = supplies_by_day.get(record.trade_day, [])
        while remaining > 0 and supplies:
            trade = supplies[0][0]
            available = int(supplies[0][1])
            used = min(available, remaining)
            ratio = Decimal(used) / Decimal(abs(trade.qty))
            if record.source == QMT3_SOURCE:
                base_id = _base_ib_trade_id(trade.id)
                linked.append(
                    IbSlice(
                        trade_id=base_id,
                        dt=trade.dt,
                        side="SELL",
                        qty=used,
                        price=trade.price,
                        gross=trade.gross * ratio,
                        commission=trade.commission * ratio,
                        role="qmt3_carried_open",
                    )
                )
                reserved[base_id] = reserved.get(base_id, 0) + used
            remaining -= used
            supplies[0][1] = available - used
            if supplies[0][1] == 0:
                supplies.pop(0)
        if record.source == QMT3_SOURCE:
            hedges[_qmt_record_id(record)] = Qmt3OpenHedge(target, tuple(linked))
            totals = qmt3_totals.setdefault(record.trade_day, [0, 0])
            totals[0] += target
            totals[1] += sum(item.qty for item in linked)

    warnings = tuple(
        f"{day:%Y-%m-%d} QMT3原始开仓应配 {target:,} 股，已按活动报表顺序关联 {actual:,} 股，缺口 {target - actual:,} 股"
        for day, (target, actual) in sorted(qmt3_totals.items())
        if actual < target
    )
    return hedges, reserved, warnings


def _build_qmt3_ledger(
    qmt3_records: list[QmtRecord],
    transfer_seller_by_receiver: dict[tuple[str, int], QmtRecord],
    qmt3_open_hedges: dict[tuple[str, int], Qmt3OpenHedge] | None = None,
) -> tuple[dict[tuple[str, int], AccountTransfer], list[VenueClose]]:
    qmt3_open_hedges = qmt3_open_hedges or {}
    lots: list[_InventoryLot] = []
    transfer_receiver_by_seller = {
        _qmt_record_id(sell): receiver_id
        for receiver_id, sell in transfer_seller_by_receiver.items()
    }
    transfers: dict[tuple[str, int], AccountTransfer] = {}
    venue_closes: list[VenueClose] = []
    for record in sorted(qmt3_records, key=_record_sort_key):
        if record.action == "证券买入" and record.qty > 0:
            open_hedge = qmt3_open_hedges.get(_qmt_record_id(record))
            lots.append(
                _InventoryLot(
                    source=record.source,
                    trade_day=record.trade_day,
                    contract_no=record.contract_no,
                    qty=record.qty,
                    cost=abs(record.amount),
                    qmt3_hedge_target=open_hedge.target_qty if open_hedge else 0,
                    qmt3_hedge_open=list(open_hedge.slices) if open_hedge else [],
                )
            )
            continue
        if record.action != "证券卖出" or record.qty <= 0:
            continue
        cost, matches, shortfall = _consume_inventory(lots, record.qty)
        receiver_id = transfer_receiver_by_seller.get(_qmt_record_id(record))
        if receiver_id is None:
            venue_closes.append(
                VenueClose(
                    source=record.source,
                    trade_day=record.trade_day,
                    contract_no=record.contract_no,
                    qty=record.qty,
                    proceeds=record.amount,
                    cost=cost,
                    pnl=record.amount - cost,
                    matches=matches,
                    inventory_shortfall=shortfall,
                    trade_dt=record.trade_dt,
                    hedge_target=hedge_target_from_shares_per_cu(record.qty, Decimal(DEFAULT_HEDGE_SHARES)),
                    qmt3_hedge_target=sum(item.qmt3_hedge_target for item in matches),
                    qmt3_hedge_open=tuple(
                        hedge for item in matches for hedge in item.qmt3_hedge_open
                    ),
                    id=_venue_close_id(record),
                )
            )
            continue
        # The receiver record is injected by build_domestic_ledger after this
        # pass. Store the source leg and FIFO result here first.
        transfers[receiver_id] = AccountTransfer(
            trade_day=record.trade_day,
            qty=record.qty,
            sell_source=record.source,
            sell_contract_no=record.contract_no,
            sell_proceeds=record.amount,
            sell_fifo_cost=cost,
            realized_pnl=record.amount - cost,
            buy_source="",
            buy_contract_no="",
            buy_cost=Decimal("0"),
            contract_gap=None,
            matches=matches,
            inventory_shortfall=shortfall,
            carried_cost=None,
            kind="QMT3 成本承接",
            qmt3_hedge_target=sum(item.qmt3_hedge_target for item in matches),
            qmt3_hedge_open=tuple(
                hedge for item in matches for hedge in item.qmt3_hedge_open
            ),
        )
    return transfers, venue_closes


def build_domestic_ledger(
    records: list[QmtRecord],
    transfer_contract_gap: int = DEFAULT_TRANSFER_CONTRACT_GAP,
    qmt3_open_hedges: dict[tuple[str, int], Qmt3OpenHedge] | None = None,
) -> tuple[list[BasketResult], list[VenueClose], list[AccountTransfer]]:
    lots: list[_InventoryLot] = []
    baskets: list[BasketResult] = []
    venue_closes: list[VenueClose] = []
    account_transfers: list[AccountTransfer] = []
    redeem_records = [item for item in records if item.source in REDEMPTION_SOURCES]
    qmt3_records = [item for item in records if item.source == QMT3_SOURCE]
    transfer_matches = identify_account_transfers(redeem_records, transfer_contract_gap)
    occupied_buy_ids = {_qmt_record_id(item) for item in transfer_matches.values()}
    qmt3_transfer_sellers = identify_qmt3_transfer_receivers(records, occupied_buy_ids)
    qmt3_transfers, qmt3_venue_closes = _build_qmt3_ledger(
        qmt3_records,
        qmt3_transfer_sellers,
        qmt3_open_hedges,
    )
    venue_closes.extend(qmt3_venue_closes)
    transferred_hedges: dict[tuple[str, int], tuple[int, tuple[IbSlice, ...]]] = {}
    for record in sorted(redeem_records, key=_record_sort_key):
        if record.action == "证券买入" and record.qty > 0:
            qmt3_transfer = qmt3_transfers.get(_qmt_record_id(record))
            transferred_target, transferred_open = transferred_hedges.get(_qmt_record_id(record), (0, ()))
            lot_cost = abs(record.amount)
            if qmt3_transfer is not None and not qmt3_transfer.inventory_shortfall:
                # Roll QMT3's original FIFO and the two-system execution
                # slippage/fees into the redeemable QMT1/QMT2 cost basis.
                lot_cost = qmt3_transfer.sell_fifo_cost + abs(record.amount) - qmt3_transfer.sell_proceeds
                qmt3_transfers[_qmt_record_id(record)] = replace(
                    qmt3_transfer,
                    buy_source=record.source,
                    buy_contract_no=record.contract_no,
                    buy_cost=abs(record.amount),
                    carried_cost=lot_cost,
                )
            elif qmt3_transfer is not None:
                qmt3_transfers[_qmt_record_id(record)] = replace(
                    qmt3_transfer,
                    buy_source=record.source,
                    buy_contract_no=record.contract_no,
                    buy_cost=abs(record.amount),
                    carried_cost=abs(record.amount),
                )
            if qmt3_transfer is not None:
                transferred_target = qmt3_transfer.qmt3_hedge_target
                transferred_open = qmt3_transfer.qmt3_hedge_open
            lots.append(
                _InventoryLot(
                    source=record.source,
                    trade_day=record.trade_day,
                    contract_no=record.contract_no,
                    qty=record.qty,
                    cost=lot_cost,
                    qmt3_hedge_target=transferred_target,
                    qmt3_hedge_open=list(transferred_open),
                )
            )
            continue
        if record.action == "证券卖出" and record.qty > 0:
            cost, matches, shortfall = _consume_inventory(lots, record.qty)
            transfer_buy = transfer_matches.get(_qmt_record_id(record))
            if transfer_buy is not None:
                carried_target = sum(item.qmt3_hedge_target for item in matches)
                carried_open = tuple(
                    hedge for item in matches for hedge in item.qmt3_hedge_open
                )
                transferred_hedges[_qmt_record_id(transfer_buy)] = (carried_target, carried_open)
                account_transfers.append(
                    AccountTransfer(
                        trade_day=record.trade_day,
                        qty=record.qty,
                        sell_source=record.source,
                        sell_contract_no=record.contract_no,
                        sell_proceeds=record.amount,
                        sell_fifo_cost=cost,
                        realized_pnl=record.amount - cost,
                        buy_source=transfer_buy.source,
                        buy_contract_no=transfer_buy.contract_no,
                        buy_cost=abs(transfer_buy.amount),
                        contract_gap=_contract_distance(transfer_buy.contract_no, record.contract_no),
                        matches=matches,
                        inventory_shortfall=shortfall,
                        carried_cost=abs(transfer_buy.amount),
                        qmt3_hedge_target=carried_target,
                        qmt3_hedge_open=carried_open,
                    )
                )
                continue
            venue_closes.append(
                VenueClose(
                    source=record.source,
                    trade_day=record.trade_day,
                    contract_no=record.contract_no,
                    qty=record.qty,
                    proceeds=record.amount,
                    cost=cost,
                    pnl=record.amount - cost,
                    matches=matches,
                    inventory_shortfall=shortfall,
                    trade_dt=record.trade_dt,
                    hedge_target=hedge_target_from_shares_per_cu(record.qty, Decimal(DEFAULT_HEDGE_SHARES)),
                    qmt3_hedge_target=sum(item.qmt3_hedge_target for item in matches),
                    qmt3_hedge_open=tuple(
                        hedge for item in matches for hedge in item.qmt3_hedge_open
                    ),
                    id=_venue_close_id(record),
                )
            )
            continue
        if record.action == "ETF 基金赎回" and record.qty > 0:
            cost, matches, shortfall = _consume_inventory(lots, record.qty)
            baskets.append(
                BasketResult(
                    id=_basket_id(record),
                    sequence=len(baskets) + 1,
                    source=record.source,
                    redeem_day=record.trade_day,
                    contract_no=record.contract_no,
                    redeem_qty=record.qty,
                    domestic_cost=cost,
                    domestic_matches=matches,
                    inventory_shortfall=shortfall,
                    hedge_target=(record.qty * DEFAULT_HEDGE_SHARES + DEFAULT_REDEMPTION_UNIT - 1)
                    // DEFAULT_REDEMPTION_UNIT,
                    qmt3_hedge_target=sum(item.qmt3_hedge_target for item in matches),
                    qmt3_hedge_open=tuple(
                        hedge for item in matches for hedge in item.qmt3_hedge_open
                    ),
                )
            )
    account_transfers.extend(qmt3_transfers.values())
    account_transfers.sort(
        key=lambda item: (
            item.trade_day,
            contract_sort_key(item.sell_contract_no),
            item.sell_source,
            contract_sort_key(item.buy_contract_no),
        )
    )
    venue_closes.sort(key=lambda item: (item.trade_day, contract_sort_key(item.contract_no), item.source))
    return baskets, venue_closes, account_transfers


def _expected_flow_day(
    action: str,
    basket: BasketResult,
    cash_difference_holidays: frozenset[date],
    refund_holidays: frozenset[date],
) -> date:
    if action == "ETF 现金差额":
        return trading_day_offset(basket.redeem_day, 3, cash_difference_holidays)
    if action == "ETF 申购退款":
        return trading_day_offset(basket.redeem_day, 6, refund_holidays)
    return basket.redeem_day


def _cash_flow_assignment_score(
    flow: CashFlow,
    basket: BasketResult,
    cash_difference_holidays: frozenset[date],
    refund_holidays: frozenset[date],
) -> tuple[int, int, date, int]:
    expected_day = _expected_flow_day(flow.action, basket, cash_difference_holidays, refund_holidays)
    early_penalty = 0 if flow.trade_day >= expected_day else 1
    return (abs((flow.trade_day - expected_day).days), early_penalty, expected_day, basket.sequence)


def attach_cash_flows(
    baskets: list[BasketResult],
    records: list[QmtRecord],
    refund_holidays: frozenset[date] = frozenset(),
    cash_difference_holidays: frozenset[date] = frozenset(),
) -> None:
    flows: dict[str, list[CashFlow]] = {basket.id: [] for basket in baskets}
    for record in records:
        if record.action not in {"ETF 申购退款", "ETF 现金差额"}:
            continue
        flow = CashFlow(
            source=record.source,
            trade_day=record.trade_day,
            contract_no=record.contract_no,
            action=record.action,
            amount=record.amount,
            row_number=record.row_number,
        )
        candidates = [
            basket
            for basket in baskets
            if basket.source == record.source
            and basket.contract_no == record.contract_no
            and flow.trade_day >= basket.redeem_day
        ]
        if not candidates:
            continue
        basket = min(
            candidates,
            key=lambda item: _cash_flow_assignment_score(
                flow,
                item,
                cash_difference_holidays,
                refund_holidays,
            ),
        )
        flows[basket.id].append(flow)
    for basket in baskets:
        basket_flows = tuple(
            sorted(flows.get(basket.id, []), key=lambda item: (item.trade_day, item.row_number))
        )
        basket.cash_flows = basket_flows
        basket.refund_amount = sum(
            (item.amount for item in basket_flows if item.action == "ETF 申购退款"), Decimal("0")
        )
        basket.cash_difference = sum(
            (item.amount for item in basket_flows if item.action == "ETF 现金差额"), Decimal("0")
        )
        cash_difference_days = [item.trade_day for item in basket_flows if item.action == "ETF 现金差额"]
        refund_days = [item.trade_day for item in basket_flows if item.action == "ETF 申购退款"]
        basket.actual_cash_difference_day = max(cash_difference_days, default=None)
        basket.actual_refund_day = max(refund_days, default=None)
        basket.domestic_pnl = basket.refund_amount + basket.cash_difference - basket.domestic_cost


def apply_hedge_targets(
    baskets: list[BasketResult],
    hedge_targets: dict[str, int] | None,
) -> None:
    if not hedge_targets:
        return
    for basket in baskets:
        if basket.id not in hedge_targets:
            continue
        target = int(hedge_targets[basket.id])
        if target < 0:
            raise ValueError(f"篮子 {basket.id} 的 IB 对冲目标不能为负数")
        basket.hedge_target = target


def load_overrides(path: Path | str) -> dict[str, dict[str, object]]:
    file_path = Path(path)
    if not file_path.exists():
        return {}
    try:
        payload = json.loads(file_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def save_overrides(path: Path | str, payload: dict[str, dict[str, object]]) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _selected_ids(override: dict[str, object] | None, key: str) -> tuple[str, ...] | None:
    if not override or key not in override:
        return None
    value = override.get(key)
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value if str(item).strip())


def _manual_virtual_close(override: dict[str, object] | None) -> bool:
    if not override:
        return False
    value = override.get("manual_virtual_close")
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "是"}


def manual_refund_override_amount(override: dict[str, object] | None) -> Decimal | None:
    if not override or "manual_refund_amount" not in override:
        return None
    value = d(override.get("manual_refund_amount"))
    if value <= 0:
        return None
    return money(value)


def apply_manual_refund_overrides(
    baskets: list[BasketResult],
    overrides: dict[str, dict[str, object]],
) -> None:
    for basket in baskets:
        manual_refund = manual_refund_override_amount(overrides.get(basket.id))
        basket.manual_refund_amount = manual_refund
        basket.manual_refund_applied = False
        if manual_refund is None:
            continue
        has_actual_refund = any(item.action == "ETF 申购退款" for item in basket.cash_flows)
        if has_actual_refund:
            continue
        basket.refund_amount = manual_refund
        basket.manual_refund_applied = True
        basket.domestic_pnl = basket.refund_amount + basket.cash_difference - basket.domestic_cost


def _trade_role(trade: IbTrade) -> str:
    marker = trade.marker.upper()
    if "AUTO_DOMESTIC_ROLLOVER_CLOSE" in marker:
        return "domestic_rollover_close"
    if "AUTO_DOMESTIC_ROLLOVER_OPEN" in marker:
        return "domestic_rollover_open"
    if "AUTO_SYNTHETIC_OPEN" in marker:
        return "synthetic_open"
    if "AUTO_DIRECT_OPEN" in marker:
        return "direct_open"
    if "AUTO_DIRECT_CLOSE" in marker:
        return "direct_close"
    return ""


def _trade_selected(trade_id: str, selected: set[str]) -> bool:
    return trade_id in selected or trade_id.split(":", 1)[0] in selected


def _selection_key(trade_id: str) -> str:
    if trade_id.startswith("domestic-rollover:"):
        return trade_id
    return trade_id.split(":", 1)[0]


def _selection_conflicts(trade_ids: Iterable[str], protected_ids: set[str]) -> set[str]:
    protected_keys = {_selection_key(item) for item in protected_ids}
    return {
        item
        for item in trade_ids
        if _selection_key(item) in protected_keys
    }


def ib_statement_to_china_dt(value: datetime) -> datetime:
    return value.replace(tzinfo=IB_STATEMENT_TZ).astimezone(CHINA_TZ).replace(tzinfo=None)


def china_dt_to_ib_statement_dt(value: datetime) -> datetime:
    return value.replace(tzinfo=CHINA_TZ).astimezone(IB_STATEMENT_TZ).replace(tzinfo=None)


def _basket_ib_cutoff(redeem_day: date) -> datetime:
    return datetime.combine(redeem_day, BASKET_IB_CUTOFF)


def _is_china_session_trade(trade: IbTrade, day: date) -> bool:
    china_dt = ib_statement_to_china_dt(trade.dt)
    return china_dt.date() == day and CHINA_SESSION_START <= china_dt.time() <= CHINA_SESSION_END


def _venue_buy_sort_key(close: VenueClose) -> Callable[[IbTrade], tuple[object, ...]]:
    target_dt = close.trade_dt

    def key(trade: IbTrade) -> tuple[object, ...]:
        china_dt = ib_statement_to_china_dt(trade.dt)
        if target_dt is not None:
            distance = abs((china_dt - target_dt).total_seconds())
            early_penalty = 0 if china_dt >= target_dt else 1
            return (distance, early_penalty, china_dt, trade.price, trade.row_number, trade.id)
        return (china_dt, trade.price, trade.row_number, trade.id)

    return key


def _take_ib_slices(
    trades: Iterable[IbTrade],
    target: int,
    capacities: dict[str, int],
    *,
    selected_ids: tuple[str, ...] | None,
    reserved_ids: set[str],
    min_dt: datetime | None = None,
    max_dt: datetime | None = None,
    predicate: Callable[[IbTrade], bool] | None = None,
) -> tuple[tuple[IbSlice, ...], int]:
    remaining = target
    slices: list[IbSlice] = []
    selected = set(selected_ids or ())
    manual = selected_ids is not None
    for trade in trades:
        if remaining <= 0:
            break
        if min_dt is not None and trade.dt < min_dt:
            continue
        if max_dt is not None and trade.dt > max_dt:
            continue
        if predicate is not None and not predicate(trade):
            continue
        if manual and not _trade_selected(trade.id, selected):
            continue
        if not manual and trade.id in reserved_ids:
            continue
        available = capacities.get(trade.id, 0)
        if available <= 0:
            continue
        used = min(available, remaining)
        ratio = Decimal(used) / Decimal(abs(trade.qty))
        slices.append(
            IbSlice(
                trade_id=trade.id,
                dt=trade.dt,
                side=trade.side,
                qty=used,
                price=trade.price,
                gross=trade.gross * ratio,
                commission=trade.commission * ratio,
                role=_trade_role(trade),
            )
        )
        capacities[trade.id] = available - used
        remaining -= used
    return tuple(slices), remaining


def _apply_ib_pnl_to_venue_close(close: VenueClose, fx_rate: Decimal) -> None:
    sell_gross = sum((item.gross for item in close.ib_open), Decimal("0"))
    sell_commission = sum((item.commission for item in close.ib_open), Decimal("0"))
    buy_gross = sum((item.gross for item in close.ib_close), Decimal("0"))
    buy_commission = sum((item.commission for item in close.ib_close), Decimal("0"))
    close.ib_trade_pnl_usd = sell_gross - sell_commission - buy_gross - buy_commission
    close.ib_pnl_cny = close.ib_trade_pnl_usd * fx_rate


def _apply_base_sell_reservations(
    sell_trades: list[IbTrade],
    sell_capacity: dict[str, int],
    reserved_qty: dict[str, int],
) -> None:
    remaining = dict(reserved_qty)
    for trade in sell_trades:
        base_id = _base_ib_trade_id(trade.id)
        wanted = remaining.get(base_id, 0)
        if wanted <= 0:
            continue
        available = sell_capacity.get(trade.id, 0)
        used = min(available, wanted)
        sell_capacity[trade.id] = available - used
        remaining[base_id] = wanted - used


def _release_base_sell_reservations(
    sell_trades: list[IbTrade],
    sell_capacity: dict[str, int],
    slices: Iterable[IbSlice],
) -> None:
    release_by_base: dict[str, int] = {}
    for item in slices:
        base_id = _base_ib_trade_id(item.trade_id)
        release_by_base[base_id] = release_by_base.get(base_id, 0) + item.qty
    for trade in sell_trades:
        base_id = _base_ib_trade_id(trade.id)
        wanted = release_by_base.get(base_id, 0)
        if wanted <= 0:
            continue
        current = sell_capacity.get(trade.id, 0)
        room = abs(trade.qty) - current
        restored = min(room, wanted)
        sell_capacity[trade.id] = current + restored
        release_by_base[base_id] = wanted - restored


def _apply_exact_sell_reservations(
    sell_capacity: dict[str, int],
    reserved_qty: dict[str, int],
) -> None:
    for trade_id, wanted in reserved_qty.items():
        available = sell_capacity.get(trade_id, 0)
        sell_capacity[trade_id] = available - min(available, max(0, wanted))


def _release_exact_sell_reservations(
    sell_trades: list[IbTrade],
    sell_capacity: dict[str, int],
    slices: Iterable[IbSlice],
) -> None:
    total_by_id = {trade.id: abs(trade.qty) for trade in sell_trades}
    for item in slices:
        current = sell_capacity.get(item.trade_id, 0)
        sell_capacity[item.trade_id] = min(
            total_by_id.get(item.trade_id, current + item.qty),
            current + item.qty,
        )


def _limited_carried_open(
    slices: tuple[IbSlice, ...],
    target: int,
) -> tuple[IbSlice, ...]:
    limited, _shortfall = _consume_carried_ib_slices(list(slices), max(0, target))
    return limited


def _basket_carried_open(
    basket: BasketResult,
    target: int,
) -> tuple[IbSlice, ...]:
    combined = tuple(
        sorted(
            (*basket.qmt3_hedge_open, *basket.domestic_rollover_open),
            key=lambda item: (item.dt, item.trade_id, item.role),
        )
    )
    return _limited_carried_open(combined, target)


def _prepare_venue_ib_events(
    venue_closes: list[VenueClose],
    buy_trades: list[IbTrade],
    buy_capacity: dict[str, int],
    reserved_buy: set[str],
    overrides: dict[str, dict[str, object]],
    basket_reserved_buy: set[str],
) -> list[tuple[datetime, VenueClose]]:
    events: list[tuple[datetime, VenueClose]] = []
    ordered_closes = sorted(
        venue_closes,
        key=lambda item: (
            item.trade_day,
            item.trade_dt or datetime.combine(item.trade_day, time.max),
            contract_sort_key(item.contract_no),
            item.source,
        ),
    )
    for close in ordered_closes:
        override = overrides.get(close.id, {})
        selected_open = _selected_ids(override, "open_trade_ids")
        selected_close = _selected_ids(override, "close_trade_ids")
        close.manual_ib_mapping = selected_open is not None or selected_close is not None
        warnings: list[str] = []
        close.hedge_target = close.hedge_target or hedge_target_from_shares_per_cu(
            close.qty,
            Decimal(DEFAULT_HEDGE_SHARES),
        )
        if selected_close is not None:
            conflicts = _selection_conflicts(selected_close, basket_reserved_buy)
            if conflicts:
                warnings.append(
                    "人工平仓选择与篮子人工映射冲突，已保护篮子并忽略："
                    + ", ".join(sorted(conflicts))
                )
            selected_close = tuple(item for item in selected_close if item not in conflicts)
            candidates = sorted(buy_trades, key=lambda item: (item.dt, item.row_number, item.id))
        else:
            candidates = sorted(
                (
                    item for item in buy_trades
                    if _is_china_session_trade(item, close.trade_day)
                ),
                key=_venue_buy_sort_key(close),
            )
        close.ib_close, close.ib_close_shortfall = _take_ib_slices(
            candidates,
            close.hedge_target,
            buy_capacity,
            selected_ids=selected_close,
            reserved_ids=reserved_buy,
        )
        close.warnings = tuple(warnings)
        matched_qty = close.hedge_target - close.ib_close_shortfall
        if matched_qty <= 0:
            continue
        event_dt = max((item.dt for item in close.ib_close), default=None)
        if event_dt is None and close.trade_dt is not None:
            event_dt = china_dt_to_ib_statement_dt(close.trade_dt)
        if event_dt is not None:
            events.append((event_dt, close))
    return events


def allocate_ib(
    baskets: list[BasketResult],
    venue_closes: list[VenueClose],
    trades: list[IbTrade],
    overrides: dict[str, dict[str, object]],
    fx_rate: Decimal = Decimal("0"),
    qmt3_reserved_sell_qty: dict[str, int] | None = None,
    domestic_rollover_reserved_sell_qty: dict[str, int] | None = None,
) -> tuple[dict[str, int], dict[str, int]]:
    sell_trades = [item for item in trades if item.qty < 0]
    buy_trades = [item for item in trades if item.qty > 0]
    sell_capacity = {item.id: abs(item.qty) for item in sell_trades}
    buy_capacity = {item.id: abs(item.qty) for item in buy_trades}
    basket_ids = {item.id for item in baskets}
    basket_reserved_sell = {
        trade_id
        for basket_id, override in overrides.items()
        for trade_id in (_selected_ids(override, "open_trade_ids") or ())
        if basket_id in basket_ids
    }
    basket_reserved_buy = {
        trade_id
        for basket_id, override in overrides.items()
        for trade_id in (_selected_ids(override, "close_trade_ids") or ())
        if basket_id in basket_ids and not _manual_virtual_close(override)
    }
    venue_ids = {item.id for item in venue_closes}
    venue_reserved_sell = {
        trade_id
        for venue_id, override in overrides.items()
        for trade_id in (_selected_ids(override, "open_trade_ids") or ())
        if venue_id in venue_ids
    }
    venue_reserved_buy = {
        trade_id
        for venue_id, override in overrides.items()
        for trade_id in (_selected_ids(override, "close_trade_ids") or ())
        if venue_id in venue_ids
    }
    reserved_sell = basket_reserved_sell | venue_reserved_sell
    reserved_buy = basket_reserved_buy | venue_reserved_buy
    _apply_base_sell_reservations(
        sell_trades,
        sell_capacity,
        qmt3_reserved_sell_qty or {},
    )
    _apply_exact_sell_reservations(
        sell_capacity,
        domestic_rollover_reserved_sell_qty or {},
    )
    venue_events = _prepare_venue_ib_events(
        venue_closes,
        buy_trades,
        buy_capacity,
        reserved_buy,
        overrides,
        basket_reserved_buy,
    )
    basket_events = [(_basket_ib_cutoff(basket.redeem_day), basket) for basket in baskets]
    events = [(moment, "venue", close) for moment, close in venue_events]
    events.extend((moment, "basket", basket) for moment, basket in basket_events)
    events.sort(
        key=lambda item: (
            item[0],
            0 if item[1] == "venue" else 1,
            item[2].trade_day if item[1] == "venue" else item[2].redeem_day,
            contract_sort_key(item[2].contract_no),
        )
    )
    for moment, kind, payload in events:
        if kind == "venue":
            close = payload
            target = close.hedge_target - close.ib_close_shortfall
            override = overrides.get(close.id, {})
            selected_open = _selected_ids(override, "open_trade_ids")
            warnings = list(close.warnings)
            if selected_open is not None:
                conflicts = _selection_conflicts(selected_open, basket_reserved_sell)
                if conflicts:
                    warnings.append(
                        "人工开仓选择与篮子人工映射冲突，已保护篮子并忽略："
                        + ", ".join(sorted(conflicts))
                    )
                safe_selected = tuple(item for item in selected_open if item not in conflicts)
                _release_base_sell_reservations(
                    sell_trades,
                    sell_capacity,
                    close.qmt3_hedge_open,
                )
                close.ib_open, close.ib_open_shortfall = _take_ib_slices(
                    sell_trades,
                    target,
                    sell_capacity,
                    selected_ids=safe_selected,
                    reserved_ids=reserved_sell,
                )
            else:
                carried_open = _limited_carried_open(close.qmt3_hedge_open, target)
                auto_open, close.ib_open_shortfall = _take_ib_slices(
                    sell_trades,
                    max(0, target - sum(item.qty for item in carried_open)),
                    sell_capacity,
                    selected_ids=None,
                    reserved_ids=reserved_sell,
                    max_dt=moment,
                )
                close.ib_open = carried_open + auto_open
            close.warnings = tuple(warnings)
            _apply_ib_pnl_to_venue_close(close, fx_rate)
            continue
        basket = payload
        override = overrides.get(basket.id)
        manual_virtual_close = _manual_virtual_close(override)
        selected_open = _selected_ids(override, "open_trade_ids")
        selected_close = None if manual_virtual_close else _selected_ids(override, "close_trade_ids")
        basket.manual_virtual_close = manual_virtual_close
        basket.manual_ib_mapping = selected_open is not None or selected_close is not None or manual_virtual_close
        basket.qmt3_open_overridden = selected_open is not None and bool(basket.qmt3_hedge_open)
        if selected_open is not None:
            _release_base_sell_reservations(sell_trades, sell_capacity, basket.qmt3_hedge_open)
            _release_exact_sell_reservations(
                sell_trades,
                sell_capacity,
                basket.domestic_rollover_open,
            )
            basket.ib_open, basket.ib_open_shortfall = _take_ib_slices(
                sell_trades,
                basket.hedge_target,
                sell_capacity,
                selected_ids=selected_open,
                reserved_ids=reserved_sell,
            )
        else:
            carried_open = _basket_carried_open(basket, basket.hedge_target)
            auto_open, basket.ib_open_shortfall = _take_ib_slices(
                sell_trades,
                max(0, basket.hedge_target - sum(item.qty for item in carried_open)),
                sell_capacity,
                selected_ids=None,
                reserved_ids=reserved_sell,
                max_dt=moment,
            )
            basket.ib_open = carried_open + auto_open
        min_close_dt = min((item.dt for item in basket.ib_open), default=None) if selected_close is not None else moment
        close_predicate = None
        if manual_virtual_close:
            prefix = f"domestic-rollover:{basket.id}:"

            def close_predicate(trade: IbTrade, prefix: str = prefix) -> bool:
                return trade.id.startswith(prefix) and _trade_role(trade) == "domestic_rollover_close"

        basket.ib_close, basket.ib_close_shortfall = _take_ib_slices(
            buy_trades,
            basket.hedge_target,
            buy_capacity,
            selected_ids=selected_close,
            reserved_ids=reserved_buy,
            min_dt=min_close_dt,
            predicate=close_predicate,
        )
        sell_gross = sum((item.gross for item in basket.ib_open), Decimal("0"))
        sell_commission = sum((item.commission for item in basket.ib_open), Decimal("0"))
        buy_gross = sum((item.gross for item in basket.ib_close), Decimal("0"))
        buy_commission = sum((item.commission for item in basket.ib_close), Decimal("0"))
        basket.ib_trade_pnl_usd = sell_gross - sell_commission - buy_gross - buy_commission
    return sell_capacity, buy_capacity


def _ib_capacity_slice(trade: IbTrade, qty: int) -> IbSlice:
    ratio = Decimal(qty) / Decimal(abs(trade.qty))
    return IbSlice(
        trade_id=trade.id,
        dt=trade.dt,
        side=trade.side,
        qty=qty,
        price=trade.price,
        gross=trade.gross * ratio,
        commission=trade.commission * ratio,
        role=_trade_role(trade),
    )


def _split_ib_slice(item: IbSlice, qty: int) -> tuple[IbSlice, IbSlice | None]:
    used = min(max(0, qty), item.qty)
    if used >= item.qty:
        return item, None
    ratio = Decimal(used) / Decimal(item.qty)
    used_slice = replace(
        item,
        qty=used,
        gross=item.gross * ratio,
        commission=item.commission * ratio,
    )
    remainder = replace(
        item,
        qty=item.qty - used,
        gross=item.gross - used_slice.gross,
        commission=item.commission - used_slice.commission,
    )
    return used_slice, remainder


def match_ib_self_closes(
    trades: list[IbTrade],
    sell_capacity: dict[str, int],
    buy_capacity: dict[str, int],
    fx_rate: Decimal,
) -> tuple[tuple[IbSelfClose, ...], tuple[IbSlice, ...]]:
    """FIFO-pair only the executable IB residual left after every business allocation."""
    residual = [
        _ib_capacity_slice(
            trade,
            (sell_capacity if trade.qty < 0 else buy_capacity).get(trade.id, 0),
        )
        for trade in trades
        if (sell_capacity if trade.qty < 0 else buy_capacity).get(trade.id, 0) > 0
    ]
    open_lots: list[IbSlice] = []
    pairs: list[IbSelfClose] = []
    for item in residual:
        current: IbSlice | None = item
        while current is not None and open_lots and open_lots[0].side != current.side:
            opening_used, opening_remainder = _split_ib_slice(
                open_lots[0],
                min(open_lots[0].qty, current.qty),
            )
            closing_used, current = _split_ib_slice(current, opening_used.qty)
            if opening_remainder is None:
                open_lots.pop(0)
            else:
                open_lots[0] = opening_remainder
            sell = opening_used if opening_used.side == "SELL" else closing_used
            buy = opening_used if opening_used.side == "BUY" else closing_used
            trade_pnl_usd = (
                sell.gross
                - sell.commission
                - buy.gross
                - buy.commission
            )
            pairs.append(
                IbSelfClose(
                    sequence=len(pairs) + 1,
                    opening=opening_used,
                    closing=closing_used,
                    trade_pnl_usd=trade_pnl_usd,
                    fx_rate=fx_rate,
                )
            )
        if current is not None:
            open_lots.append(current)
    return tuple(pairs), tuple(open_lots)


def build_ib_allocation_claims(
    baskets: Iterable[BasketResult],
    venue_closes: Iterable[VenueClose],
    residual_pairs: Iterable[IbSelfClose],
    unmatched: Iterable[IbSlice],
) -> tuple[IbAllocationClaim, ...]:
    claims: list[IbAllocationClaim] = []

    def add(owner_type: str, owner_id: str, leg: str, slices: Iterable[IbSlice]) -> None:
        claims.extend(
            IbAllocationClaim(
                trade_id=item.trade_id,
                side=item.side,
                qty=item.qty,
                owner_type=owner_type,
                owner_id=owner_id,
                leg=leg,
            )
            for item in slices
            if item.qty > 0
        )

    for basket in baskets:
        add("basket", basket.id, "ib_open", basket.ib_open)
        add("basket", basket.id, "ib_close", basket.ib_close)
    for close in venue_closes:
        add("strategy_self_close", close.id, "ib_open", close.ib_open)
        add("strategy_self_close", close.id, "ib_close", close.ib_close)
    for pair in residual_pairs:
        owner_id = f"ib-residual:{pair.sequence}"
        add("ib_residual_pair", owner_id, "opening", (pair.opening,))
        add("ib_residual_pair", owner_id, "closing", (pair.closing,))
    for index, item in enumerate(unmatched, start=1):
        add("unmatched_risk", f"unmatched:{index}", "residual", (item,))
    return tuple(claims)


def _allocation_trade_key(trade_id: str) -> str:
    if trade_id.startswith("domestic-rollover:"):
        return trade_id
    return _base_ib_trade_id(trade_id)


def validate_allocation_integrity(
    records: Iterable[QmtRecord],
    ib_trades: Iterable[IbTrade],
    baskets: Iterable[BasketResult],
    venue_closes: Iterable[VenueClose],
    account_transfers: Iterable[AccountTransfer],
    claims: Iterable[IbAllocationClaim],
) -> tuple[str, ...]:
    """Fail closed if a domestic or IB quantity slice is counted more than once."""
    ib_capacity: dict[tuple[str, str], int] = {}
    for trade in ib_trades:
        key = (_allocation_trade_key(trade.id), trade.side)
        ib_capacity[key] = ib_capacity.get(key, 0) + abs(trade.qty)
    ib_used: dict[tuple[str, str], int] = {}
    for claim in claims:
        key = (_allocation_trade_key(claim.trade_id), claim.side)
        ib_used[key] = ib_used.get(key, 0) + claim.qty
    for key, used in ib_used.items():
        capacity = ib_capacity.get(key, 0)
        if used > capacity:
            raise ValueError(
                f"IB成交重复占用 {key[0]} {key[1]}：已分配 {used:,}，原始容量 {capacity:,}"
            )

    domestic_capacity: dict[tuple[str, date, int | str], int] = {}
    for record in records:
        if record.action != "证券买入" or record.qty <= 0:
            continue
        key = (record.source, record.trade_day, record.contract_no)
        domestic_capacity[key] = domestic_capacity.get(key, 0) + record.qty
    domestic_used: dict[tuple[str, date, int | str], int] = {}

    def add_matches(matches: Iterable[LotMatch]) -> None:
        for match in matches:
            key = (match.source, match.trade_day, match.contract_no)
            domestic_used[key] = domestic_used.get(key, 0) + match.qty

    for basket in baskets:
        add_matches(basket.domestic_matches)
    for close in venue_closes:
        add_matches(close.matches)
    for transfer in account_transfers:
        add_matches(transfer.matches)
    for key, used in domestic_used.items():
        capacity = domestic_capacity.get(key, 0)
        if used > capacity:
            raise ValueError(
                f"国内买入重复占用 {key[0]} {key[1]:%Y-%m-%d} {key[2]}："
                f"已分配 {used:,}，原始容量 {capacity:,}"
            )

    warnings: list[str] = []
    orphaned = [key for key in ib_used if key not in ib_capacity]
    if orphaned:
        warnings.append(
            "存在无法回溯到当前IB派生交易的占用："
            + ", ".join(f"{trade_id} {side}" for trade_id, side in orphaned)
        )
    return tuple(warnings)


def _active_borrow_qty(basket: BasketResult, value_day: date) -> int:
    opened = sum(
        item.qty
        for item in basket.ib_open
        if item.dt.date() < value_day and item.role in {"direct_open", "qmt3_carried_open"}
    )
    closed = sum(item.qty for item in basket.ib_close if item.dt.date() < value_day)
    return max(0, opened - closed)


def allocate_borrow_fees(baskets: list[BasketResult], fees: list[BorrowFee]) -> None:
    for fee in fees:
        active = {basket.id: _active_borrow_qty(basket, fee.value_day) for basket in baskets}
        total_active = sum(active.values())
        if total_active <= 0:
            continue
        for basket in baskets:
            qty = active[basket.id]
            if qty:
                basket.ib_borrow_fee_usd += fee.amount * Decimal(qty) / Decimal(total_active)


def finalize_baskets(
    baskets: list[BasketResult],
    fx_rate: Decimal,
    qmt_latest_day: date | None,
    refund_holidays: frozenset[date],
    cash_difference_holidays: frozenset[date] = frozenset(),
) -> None:
    for basket in baskets:
        warnings: list[str] = []
        basket.expected_cash_difference_day = trading_day_offset(basket.redeem_day, 3, cash_difference_holidays)
        basket.expected_refund_day = trading_day_offset(basket.redeem_day, 6, refund_holidays)
        has_cash_difference = any(item.action == "ETF 现金差额" for item in basket.cash_flows)
        has_actual_refund = any(item.action == "ETF 申购退款" for item in basket.cash_flows)
        has_refund = has_actual_refund or basket.manual_refund_applied
        if basket.inventory_shortfall:
            warnings.append(f"国内库存缺口 {basket.inventory_shortfall:,} 份")
        if basket.ib_open_shortfall:
            warnings.append(f"IB开仓缺口 {basket.ib_open_shortfall:,} 股")
        if basket.ib_close_shortfall:
            warnings.append(f"IB平仓缺口 {basket.ib_close_shortfall:,} 股")
        qmt3_actual = sum(item.qty for item in basket.qmt3_hedge_open)
        if basket.qmt3_hedge_target > qmt3_actual:
            warnings.append(
                f"QMT3承接开仓溯源缺口 {basket.qmt3_hedge_target - qmt3_actual:,} 股"
            )
        if basket.qmt3_open_overridden:
            warnings.append("人工IB映射已覆盖QMT3原始开仓承接关系")
        if basket.domestic_rollover_open:
            warnings.append(
                f"承接国内滚动虚拟开仓 {sum(item.qty for item in basket.domestic_rollover_open):,} 股"
            )
        if basket.manual_virtual_close:
            if any(item.role == "domestic_rollover_close" for item in basket.ib_close):
                warnings.append("已人工标记虚拟平仓，使用次交易日国内买入估算XOP平仓")
            else:
                warnings.append("已人工标记虚拟平仓，但未匹配到足量次交易日国内买入")
        if not has_cash_difference:
            expected = basket.expected_cash_difference_day
            if qmt_latest_day is not None and qmt_latest_day >= expected:
                warnings.append(f"未发现T+3现金差额，预计日期 {expected:%Y-%m-%d}")
        if basket.manual_refund_applied:
            warnings.append("现金替代退款使用人工输入；交割单出现后自动以交割单为准")
        elif basket.manual_refund_amount is not None and has_actual_refund:
            warnings.append("已保存人工退款，但交割单实际退款已出现，当前以交割单为准")
        if not has_refund:
            warnings.append("尚未发现现金替代退款")
        basket.ib_pnl_usd = basket.ib_trade_pnl_usd - basket.ib_borrow_fee_usd
        basket.fx_rate = fx_rate
        basket.ib_pnl_cny = basket.ib_pnl_usd * fx_rate
        basket.total_pnl_cny = basket.domestic_pnl + basket.ib_pnl_cny
        basket.warnings = tuple(warnings)
        if basket.inventory_shortfall:
            basket.status = "国内库存不足"
        elif basket.ib_open_shortfall or basket.ib_close_shortfall:
            basket.status = "IB未完全匹配"
        elif not has_cash_difference:
            basket.status = "待现金差额"
        elif not has_refund:
            basket.status = "待现金替代款"
        else:
            basket.status = "已结算"


def calculate(
    qmt_paths: dict[str, Path | str | None],
    ib_path: Path | str,
    fx_rate: Decimal | str | float,
    overrides: dict[str, dict[str, object]] | None = None,
    market_holidays: Iterable[date] = (),
    transfer_contract_gap: int = DEFAULT_TRANSFER_CONTRACT_GAP,
    hedge_targets: dict[str, int] | None = None,
    qmt_time_root: Path | str | None = None,
) -> CalculationResult:
    records = load_qmt_records(qmt_paths, qmt_time_root)
    raw_ib_trades, borrow_fees = load_ib_statement(ib_path)
    ib_stock_trades = load_ib_stock_trades(ib_path)
    ib_trades = build_ib_hedge_trades(raw_ib_trades, ib_stock_trades)
    qmt3_open_hedges, qmt3_reserved_sell_qty, qmt3_link_warnings = build_qmt3_open_hedges(
        records,
        ib_trades,
        transfer_contract_gap,
    )
    baskets, venue_closes, account_transfers = build_domestic_ledger(
        records,
        transfer_contract_gap,
        qmt3_open_hedges,
    )
    refund_holidays = frozenset(market_holidays)
    cash_difference_holidays: frozenset[date] = frozenset()
    attach_cash_flows(baskets, records, refund_holidays, cash_difference_holidays)
    apply_manual_refund_overrides(baskets, overrides or {})
    apply_hedge_targets(baskets, hedge_targets)
    fx = d(fx_rate)
    remaining_sell, remaining_buy = allocate_ib(
        baskets,
        venue_closes,
        ib_trades,
        overrides or {},
        fx,
        qmt3_reserved_sell_qty,
    )
    rollover_trades, rollover_links = _build_domestic_rollover_plan(
        baskets,
        venue_closes,
        account_transfers,
        records,
        fx,
        overrides or {},
    )
    if rollover_trades:
        attach_domestic_rollover_opens(baskets, rollover_links)
        rollover_reserved_sell_qty = {
            link.open_slice.trade_id: link.open_slice.qty
            for link in rollover_links
        }
        ib_trades = build_ib_hedge_trades(
            raw_ib_trades,
            ib_stock_trades,
            include_unmatched_buys=True,
        )
        ib_trades.extend(rollover_trades)
        ib_trades.sort(key=lambda item: (item.dt, item.row_number, item.id))
        remaining_sell, remaining_buy = allocate_ib(
            baskets,
            venue_closes,
            ib_trades,
            overrides or {},
            fx,
            qmt3_reserved_sell_qty,
            rollover_reserved_sell_qty,
        )
    residual_ib_sell_qty = sum(remaining_sell.values())
    residual_ib_buy_qty = sum(remaining_buy.values())
    ib_self_closes, unmatched_ib = match_ib_self_closes(
        ib_trades,
        remaining_sell,
        remaining_buy,
        fx,
    )
    unmatched_ib_sell_qty = sum(item.qty for item in unmatched_ib if item.side == "SELL")
    unmatched_ib_buy_qty = sum(item.qty for item in unmatched_ib if item.side == "BUY")
    allocate_borrow_fees(baskets, borrow_fees)
    qmt_latest_day = max((item.trade_day for item in records if item.source in REDEMPTION_SOURCES), default=None)
    finalize_baskets(baskets, fx, qmt_latest_day, refund_holidays, cash_difference_holidays)
    ib_allocations = build_ib_allocation_claims(
        baskets,
        venue_closes,
        ib_self_closes,
        unmatched_ib,
    )
    allocation_warnings = validate_allocation_integrity(
        records,
        ib_trades,
        baskets,
        venue_closes,
        account_transfers,
        ib_allocations,
    )
    warnings: list[str] = []
    warnings.extend(qmt3_link_warnings)
    warnings.extend(allocation_warnings)
    if not qmt_paths.get("QMT2"):
        warnings.append("QMT2未配置，按空账户处理")
    venue_shortfall = sum(item.inventory_shortfall for item in venue_closes)
    if venue_shortfall:
        warnings.append(f"场内卖出存在未匹配库存 {venue_shortfall:,} 份")
    transfer_shortfall = sum(item.inventory_shortfall for item in account_transfers)
    if transfer_shortfall:
        warnings.append(f"跨账户调仓卖出存在未匹配库存 {transfer_shortfall:,} 份")
    qmt3_transfer_shortfall = sum(
        item.inventory_shortfall
        for item in account_transfers
        if item.sell_source == QMT3_SOURCE
    )
    if qmt3_transfer_shortfall:
        warnings.append(f"QMT3移仓来源库存不足 {qmt3_transfer_shortfall:,} 份，已采用接收账户实际成本")
    return CalculationResult(
        baskets=tuple(baskets),
        venue_closes=tuple(venue_closes),
        account_transfers=tuple(account_transfers),
        qmt_records=tuple(records),
        ib_trades=tuple(ib_trades),
        borrow_fees=tuple(borrow_fees),
        unallocated_ib_sell_qty=unmatched_ib_sell_qty,
        unallocated_ib_buy_qty=unmatched_ib_buy_qty,
        qmt_latest_day=qmt_latest_day,
        ib_self_closes=ib_self_closes,
        unmatched_ib=unmatched_ib,
        residual_ib_sell_qty=residual_ib_sell_qty,
        residual_ib_buy_qty=residual_ib_buy_qty,
        ib_allocations=ib_allocations,
        allocation_warnings=allocation_warnings,
        warnings=tuple(warnings),
    )


def basket_summary_rows(result: CalculationResult) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for basket in result.baskets:
        rows.append(
            {
                "轮次": basket.sequence,
                "状态": basket.status,
                "赎回日期": basket.redeem_day.isoformat(),
                "预计现金差额日": basket.expected_cash_difference_day.isoformat() if basket.expected_cash_difference_day else "",
                "实际现金差额日": basket.actual_cash_difference_day.isoformat() if basket.actual_cash_difference_day else "",
                "预计退款日": basket.expected_refund_day.isoformat() if basket.expected_refund_day else "",
                "实际退款日": basket.actual_refund_day.isoformat() if basket.actual_refund_day else "",
                "来源账户": basket.source,
                "合同编号": basket.contract_no,
                "赎回份额": basket.redeem_qty,
                "ETF买入成本": money(basket.domestic_cost),
                "赎回退款": money(basket.refund_amount),
                "退款来源": "人工" if basket.manual_refund_applied else ("交割单" if basket.actual_refund_day else ""),
                "人工退款金额": money(basket.manual_refund_amount) if basket.manual_refund_amount is not None else "",
                "现金差额": money(basket.cash_difference),
                "国内盈亏": money(basket.domestic_pnl),
                "IB匹配股数": basket.hedge_target - basket.ib_open_shortfall,
                "QMT3承接IB目标": basket.qmt3_hedge_target,
                "QMT3承接IB已关联": sum(item.qty for item in basket.qmt3_hedge_open),
                "国内滚动承接IB目标": basket.domestic_rollover_target,
                "国内滚动承接IB已关联": sum(item.qty for item in basket.domestic_rollover_open),
                "IB交易盈亏USD": basket.ib_trade_pnl_usd.quantize(Q6, rounding=ROUND_HALF_UP),
                "IB借券费USD": money(basket.ib_borrow_fee_usd),
                "IB净盈亏USD": basket.ib_pnl_usd.quantize(Q6, rounding=ROUND_HALF_UP),
                "汇率": basket.fx_rate,
                "IB盈亏RMB": money(basket.ib_pnl_cny),
                "合计盈亏RMB": money(basket.total_pnl_cny),
                "人工IB映射": "是" if basket.manual_ib_mapping else "否",
                "人工虚拟平仓": "是" if basket.manual_virtual_close else "否",
                "提示": "；".join(basket.warnings),
            }
        )
    return rows


def strategy_self_summary_rows(result: CalculationResult) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for close in result.venue_closes:
        opening_dt = min((item.dt for item in close.ib_open), default=None)
        closing_dt = max((item.dt for item in close.ib_close), default=None)
        rows.append(
            {
                "状态": close.status,
                "平仓日期": close.trade_day.isoformat(),
                "来源账户": close.source,
                "合同编号": close.contract_no,
                "国内卖出份额": close.qty,
                "国内FIFO成本": money(close.cost),
                "国内卖出净额": money(close.proceeds),
                "国内盈亏RMB": money(close.pnl),
                "XOP目标股数": close.hedge_target,
                "IB开仓股数": close.ib_open_qty,
                "IB平仓股数": close.ib_close_qty,
                "IB开仓时间": opening_dt.isoformat(sep=" ") if opening_dt else "",
                "IB平仓时间": closing_dt.isoformat(sep=" ") if closing_dt else "",
                "IB佣金USD": close.ib_commission_usd.quantize(Q6, rounding=ROUND_HALF_UP),
                "IB盈亏USD": close.ib_trade_pnl_usd.quantize(Q6, rounding=ROUND_HALF_UP),
                "IB盈亏RMB": money(close.ib_pnl_cny),
                "自平合计RMB": money(close.total_pnl_cny),
                "人工IB映射": "是" if close.manual_ib_mapping else "否",
                "国内库存缺口": close.inventory_shortfall,
                "IB开仓缺口": close.ib_open_shortfall,
                "IB平仓缺口": close.ib_close_shortfall,
                "提示": "；".join(close.warnings),
            }
        )
    return rows
