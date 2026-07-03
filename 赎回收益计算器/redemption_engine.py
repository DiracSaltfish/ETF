from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

import pandas as pd


TARGET_CODE = "159518"
TARGET_FOREIGN_CODE = "XOP"
DEFAULT_REDEMPTION_UNIT = 1_000_000
DEFAULT_HEDGE_SHARES = 990
DEFAULT_TRANSFER_CONTRACT_GAP = 1000
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
    contract_no: int
    action: str
    qty: int
    price: Decimal
    amount: Decimal
    code: str
    name: str

    @property
    def key(self) -> tuple[str, int, date]:
        return self.source, self.contract_no, self.trade_day


@dataclass(frozen=True)
class LotMatch:
    source: str
    trade_day: date
    contract_no: int
    qty: int
    cost: Decimal


@dataclass(frozen=True)
class VenueClose:
    source: str
    trade_day: date
    contract_no: int
    qty: int
    proceeds: Decimal
    cost: Decimal
    pnl: Decimal
    matches: tuple[LotMatch, ...]
    inventory_shortfall: int = 0


@dataclass(frozen=True)
class AccountTransfer:
    trade_day: date
    qty: int
    sell_source: str
    sell_contract_no: int
    sell_proceeds: Decimal
    sell_fifo_cost: Decimal
    realized_pnl: Decimal
    buy_source: str
    buy_contract_no: int
    buy_cost: Decimal
    contract_gap: int
    matches: tuple[LotMatch, ...] = ()
    inventory_shortfall: int = 0


@dataclass(frozen=True)
class CashFlow:
    source: str
    trade_day: date
    contract_no: int
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
class IbSlice:
    trade_id: str
    dt: datetime
    side: str
    qty: int
    price: Decimal
    gross: Decimal
    commission: Decimal


@dataclass(frozen=True)
class BorrowFee:
    value_day: date
    qty: int
    amount: Decimal


@dataclass
class BasketResult:
    id: str
    sequence: int
    source: str
    redeem_day: date
    contract_no: int
    redeem_qty: int
    domestic_cost: Decimal = Decimal("0")
    domestic_matches: tuple[LotMatch, ...] = ()
    inventory_shortfall: int = 0
    refund_amount: Decimal = Decimal("0")
    cash_difference: Decimal = Decimal("0")
    cash_flows: tuple[CashFlow, ...] = ()
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
    warnings: tuple[str, ...] = ()

    @property
    def settled_baskets(self) -> tuple[BasketResult, ...]:
        return tuple(item for item in self.baskets if item.status == "已结算")

    @property
    def settled_total_cny(self) -> Decimal:
        return money(sum((item.total_pnl_cny for item in self.settled_baskets), Decimal("0")))


@dataclass
class _InventoryLot:
    source: str
    trade_day: date
    contract_no: int
    qty: int
    cost: Decimal


def _required_columns(frame: pd.DataFrame, path: Path) -> None:
    required = {"证券代码", "操作", "成交数量", "发生金额", "合同编号", "交收日期"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path.name} 缺少字段: {', '.join(missing)}")


def load_qmt_file(path: Path | str | None, source: str) -> list[QmtRecord]:
    if path is None or not str(path).strip():
        return []
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"{source} 文件不存在: {file_path}")
    frame = pd.read_excel(file_path)
    _required_columns(frame, file_path)
    records: list[QmtRecord] = []
    for row_number, row in frame.iterrows():
        if normalize_code(row.get("证券代码")) != TARGET_CODE:
            continue
        action = str(row.get("操作") or "").strip()
        if not action:
            continue
        try:
            trade_day = datetime.strptime(str(int(d(row.get("交收日期")))), "%Y%m%d").date()
            contract_no = int(d(row.get("合同编号")))
            qty = int(d(row.get("成交数量")))
        except (ValueError, TypeError):
            continue
        records.append(
            QmtRecord(
                source=source,
                row_number=int(row_number) + 2,
                trade_day=trade_day,
                contract_no=contract_no,
                action=action,
                qty=qty,
                price=d(row.get("成交均价")),
                amount=d(row.get("发生金额")),
                code=TARGET_CODE,
                name=str(row.get("证券名称") or row.get("证券中文全称") or "").strip(),
            )
        )
    return records


def load_qmt_records(qmt_paths: dict[str, Path | str | None]) -> list[QmtRecord]:
    records: list[QmtRecord] = []
    for source in sorted(qmt_paths):
        records.extend(load_qmt_file(qmt_paths[source], source))
    # The two QMT accounts are intentionally treated as one virtual inventory.
    records.sort(key=lambda item: (item.trade_day, item.contract_no, item.source, item.row_number))
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


def _consume_inventory(lots: list[_InventoryLot], target_qty: int) -> tuple[Decimal, tuple[LotMatch, ...], int]:
    remaining = max(0, target_qty)
    cost = Decimal("0")
    matches: list[LotMatch] = []
    while remaining > 0 and lots:
        lot = lots[0]
        used = min(lot.qty, remaining)
        used_cost = lot.cost * Decimal(used) / Decimal(lot.qty)
        matches.append(
            LotMatch(
                source=lot.source,
                trade_day=lot.trade_day,
                contract_no=lot.contract_no,
                qty=used,
                cost=used_cost,
            )
        )
        cost += used_cost
        lot.qty -= used
        lot.cost -= used_cost
        remaining -= used
        if lot.qty == 0:
            lots.pop(0)
    return cost, tuple(matches), remaining


def _basket_id(record: QmtRecord) -> str:
    raw = f"{record.source}|{record.trade_day:%Y%m%d}|{record.contract_no}|{record.qty}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _qmt_record_id(record: QmtRecord) -> tuple[str, int]:
    return record.source, record.row_number


def identify_account_transfers(
    records: list[QmtRecord],
    max_contract_gap: int = DEFAULT_TRANSFER_CONTRACT_GAP,
) -> dict[tuple[str, int], QmtRecord]:
    """Identify conservative cross-account opposite-side transfer pairs.

    Full QMT statements have no execution time. Contract order is therefore the
    only available intraday proxy, and only exact-quantity pairs are accepted.
    """
    trades = [item for item in records if item.action in {"证券买入", "证券卖出"} and item.qty > 0]
    buys = [item for item in trades if item.action == "证券买入"]
    used_buys: set[tuple[str, int]] = set()
    matches: dict[tuple[str, int], QmtRecord] = {}
    for sell in (item for item in trades if item.action == "证券卖出"):
        candidates = [
            buy
            for buy in buys
            if _qmt_record_id(buy) not in used_buys
            and buy.trade_day == sell.trade_day
            and buy.source != sell.source
            and buy.qty == sell.qty
            and abs(buy.contract_no - sell.contract_no) <= max_contract_gap
        ]
        if not candidates:
            continue
        buy = min(candidates, key=lambda item: (abs(item.contract_no - sell.contract_no), item.source, item.row_number))
        matches[_qmt_record_id(sell)] = buy
        used_buys.add(_qmt_record_id(buy))
    return matches


def build_domestic_ledger(
    records: list[QmtRecord],
    transfer_contract_gap: int = DEFAULT_TRANSFER_CONTRACT_GAP,
) -> tuple[list[BasketResult], list[VenueClose], list[AccountTransfer]]:
    lots: list[_InventoryLot] = []
    baskets: list[BasketResult] = []
    venue_closes: list[VenueClose] = []
    account_transfers: list[AccountTransfer] = []
    transfer_matches = identify_account_transfers(records, transfer_contract_gap)
    for record in records:
        if record.action == "证券买入" and record.qty > 0:
            lots.append(
                _InventoryLot(
                    source=record.source,
                    trade_day=record.trade_day,
                    contract_no=record.contract_no,
                    qty=record.qty,
                    cost=abs(record.amount),
                )
            )
            continue
        if record.action == "证券卖出" and record.qty > 0:
            cost, matches, shortfall = _consume_inventory(lots, record.qty)
            transfer_buy = transfer_matches.get(_qmt_record_id(record))
            if transfer_buy is not None:
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
                        contract_gap=abs(transfer_buy.contract_no - record.contract_no),
                        matches=matches,
                        inventory_shortfall=shortfall,
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
                )
            )
    return baskets, venue_closes, account_transfers


def attach_cash_flows(baskets: list[BasketResult], records: list[QmtRecord]) -> None:
    flows: dict[tuple[str, int], list[CashFlow]] = {}
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
        flows.setdefault((record.source, record.contract_no), []).append(flow)
    for basket in baskets:
        basket_flows = tuple(
            sorted(flows.get((basket.source, basket.contract_no), []), key=lambda item: (item.trade_day, item.row_number))
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


def _take_ib_slices(
    trades: Iterable[IbTrade],
    target: int,
    capacities: dict[str, int],
    *,
    selected_ids: tuple[str, ...] | None,
    reserved_ids: set[str],
    min_dt: datetime | None = None,
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
        if manual and trade.id not in selected:
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
            )
        )
        capacities[trade.id] = available - used
        remaining -= used
    return tuple(slices), remaining


def allocate_ib(
    baskets: list[BasketResult],
    trades: list[IbTrade],
    overrides: dict[str, dict[str, object]],
) -> tuple[dict[str, int], dict[str, int]]:
    sell_trades = [item for item in trades if item.qty < 0]
    buy_trades = [item for item in trades if item.qty > 0]
    sell_capacity = {item.id: abs(item.qty) for item in sell_trades}
    buy_capacity = {item.id: abs(item.qty) for item in buy_trades}
    reserved_sell = {
        trade_id
        for basket_id, override in overrides.items()
        for trade_id in (_selected_ids(override, "open_trade_ids") or ())
        if basket_id in {item.id for item in baskets}
    }
    reserved_buy = {
        trade_id
        for basket_id, override in overrides.items()
        for trade_id in (_selected_ids(override, "close_trade_ids") or ())
        if basket_id in {item.id for item in baskets}
    }
    for basket in baskets:
        override = overrides.get(basket.id)
        selected_open = _selected_ids(override, "open_trade_ids")
        selected_close = _selected_ids(override, "close_trade_ids")
        basket.manual_ib_mapping = selected_open is not None or selected_close is not None
        basket.ib_open, basket.ib_open_shortfall = _take_ib_slices(
            sell_trades,
            basket.hedge_target,
            sell_capacity,
            selected_ids=selected_open,
            reserved_ids=reserved_sell,
        )
        min_close_dt = min((item.dt for item in basket.ib_open), default=None)
        basket.ib_close, basket.ib_close_shortfall = _take_ib_slices(
            buy_trades,
            basket.hedge_target,
            buy_capacity,
            selected_ids=selected_close,
            reserved_ids=reserved_buy,
            min_dt=min_close_dt,
        )
        sell_gross = sum((item.gross for item in basket.ib_open), Decimal("0"))
        sell_commission = sum((item.commission for item in basket.ib_open), Decimal("0"))
        buy_gross = sum((item.gross for item in basket.ib_close), Decimal("0"))
        buy_commission = sum((item.commission for item in basket.ib_close), Decimal("0"))
        basket.ib_trade_pnl_usd = sell_gross - sell_commission - buy_gross - buy_commission
    return sell_capacity, buy_capacity


def _active_borrow_qty(basket: BasketResult, value_day: date) -> int:
    opened = sum(item.qty for item in basket.ib_open if item.dt.date() < value_day)
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
    holidays: frozenset[date],
) -> None:
    for basket in baskets:
        warnings: list[str] = []
        basket.expected_cash_difference_day = trading_day_offset(basket.redeem_day, 3, holidays)
        basket.expected_refund_day = trading_day_offset(basket.redeem_day, 6, holidays)
        has_cash_difference = any(item.action == "ETF 现金差额" for item in basket.cash_flows)
        has_refund = any(item.action == "ETF 申购退款" for item in basket.cash_flows)
        if basket.inventory_shortfall:
            warnings.append(f"国内库存缺口 {basket.inventory_shortfall:,} 份")
        if basket.ib_open_shortfall:
            warnings.append(f"IB开仓缺口 {basket.ib_open_shortfall:,} 股")
        if basket.ib_close_shortfall:
            warnings.append(f"IB平仓缺口 {basket.ib_close_shortfall:,} 股")
        if not has_cash_difference:
            expected = basket.expected_cash_difference_day
            if qmt_latest_day is not None and qmt_latest_day >= expected:
                warnings.append(f"未发现T+3现金差额，预计日期 {expected:%Y-%m-%d}")
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
) -> CalculationResult:
    records = load_qmt_records(qmt_paths)
    baskets, venue_closes, account_transfers = build_domestic_ledger(records, transfer_contract_gap)
    attach_cash_flows(baskets, records)
    ib_trades, borrow_fees = load_ib_statement(ib_path)
    remaining_sell, remaining_buy = allocate_ib(baskets, ib_trades, overrides or {})
    allocate_borrow_fees(baskets, borrow_fees)
    qmt_latest_day = max((item.trade_day for item in records), default=None)
    holidays = frozenset(market_holidays)
    finalize_baskets(baskets, d(fx_rate), qmt_latest_day, holidays)
    warnings: list[str] = []
    if not qmt_paths.get("QMT2"):
        warnings.append("QMT2未配置，按空账户处理")
    venue_shortfall = sum(item.inventory_shortfall for item in venue_closes)
    if venue_shortfall:
        warnings.append(f"场内卖出存在未匹配库存 {venue_shortfall:,} 份")
    transfer_shortfall = sum(item.inventory_shortfall for item in account_transfers)
    if transfer_shortfall:
        warnings.append(f"跨账户调仓卖出存在未匹配库存 {transfer_shortfall:,} 份")
    return CalculationResult(
        baskets=tuple(baskets),
        venue_closes=tuple(venue_closes),
        account_transfers=tuple(account_transfers),
        qmt_records=tuple(records),
        ib_trades=tuple(ib_trades),
        borrow_fees=tuple(borrow_fees),
        unallocated_ib_sell_qty=sum(remaining_sell.values()),
        unallocated_ib_buy_qty=sum(remaining_buy.values()),
        qmt_latest_day=qmt_latest_day,
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
                "现金差额": money(basket.cash_difference),
                "国内盈亏": money(basket.domestic_pnl),
                "IB匹配股数": basket.hedge_target - basket.ib_open_shortfall,
                "IB交易盈亏USD": basket.ib_trade_pnl_usd.quantize(Q6, rounding=ROUND_HALF_UP),
                "IB借券费USD": money(basket.ib_borrow_fee_usd),
                "IB净盈亏USD": basket.ib_pnl_usd.quantize(Q6, rounding=ROUND_HALF_UP),
                "汇率": basket.fx_rate,
                "IB盈亏RMB": money(basket.ib_pnl_cny),
                "合计盈亏RMB": money(basket.total_pnl_cny),
                "人工IB映射": "是" if basket.manual_ib_mapping else "否",
                "提示": "；".join(basket.warnings),
            }
        )
    return rows
