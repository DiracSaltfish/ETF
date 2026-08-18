from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Iterable

from india_calendar import CHINA_TZ, NEW_YORK_TZ, TradingCalendar
from india_config import IndiaConfig
from india_models import (
    HedgeSummary,
    IbFill,
    IndiaBasket,
    IndiaCalculation,
    IndiaLot,
    PositionSnapshot,
    IndiaTrade,
    LotMatch,
    RedemptionEvent,
    IndiaSettlement,
)
from india_sources import load_ib_india_fills, load_qmt_accounts


Q2 = Decimal("0.01")
NIFTY_MULTIPLIER = Decimal("2")


def money(value: Decimal) -> Decimal:
    return Decimal(value).quantize(Q2, rounding=ROUND_HALF_UP)


def _record_cost(record: IndiaTrade) -> Decimal:
    amount = abs(record.amount)
    if amount:
        return amount
    return abs(record.price) * record.qty


def _consume(
    lots: list[IndiaLot],
    qty: int,
    *,
    mature_only: bool = False,
    as_of: date | None = None,
) -> tuple[tuple[LotMatch, ...], int]:
    remaining = qty
    matches: list[LotMatch] = []
    for lot in lots:
        if remaining <= 0:
            break
        if lot.remaining_qty <= 0:
            continue
        if mature_only and (as_of is None or lot.eligible_day > as_of):
            continue
        used = min(remaining, lot.remaining_qty)
        lot.remaining_qty -= used
        remaining -= used
        matches.append(LotMatch(lot.lot_id, lot.buy_day, used, money(lot.cost_per_share * used)))
    return tuple(matches), remaining


def _event_order(day: date, dt: datetime | None, priority: int, sequence: int) -> tuple[datetime, int, int]:
    timestamp = dt or datetime.combine(day, datetime.max.time() if priority else datetime.min.time())
    return timestamp, priority, sequence


def _qmt_redemption_events(records: Iterable[IndiaTrade]) -> list[RedemptionEvent]:
    result: list[RedemptionEvent] = []
    for record in records:
        if record.action != "REDEEM":
            continue
        result.append(
            RedemptionEvent(
                event_id=f"qmt:{record.account}:{record.trade_day.isoformat()}:{record.row_number}",
                account=record.account,
                redeem_day=record.trade_day,
                qty=record.qty,
                source="qmt_statement",
                contract_no=record.contract_no,
                event_dt=record.event_dt,
            )
        )
    return result


def merge_redemptions(records: Iterable[IndiaTrade], extra: Iterable[RedemptionEvent]) -> tuple[RedemptionEvent, ...]:
    """Prefer imported/manual data over the QMT row when the same redemption appears twice."""
    candidates = _qmt_redemption_events(records) + list(extra)
    priority = {"statement": 0, "manual": 1, "qmt_statement": 2}
    selected: dict[tuple[str, date, int, str], RedemptionEvent] = {}
    for event in candidates:
        key = (event.account, event.redeem_day, event.qty, event.contract_no)
        current = selected.get(key)
        if current is None or priority.get(event.source, 9) < priority.get(current.source, 9):
            selected[key] = event
    return tuple(sorted(selected.values(), key=lambda item: (item.redeem_day, item.account, item.effective_dt, item.event_id)))


def account_inventory_snapshot(
    records: Iterable[IndiaTrade],
    calendar: TradingCalendar,
    as_of: date,
    *,
    holding_days: int = 3,
    redemptions: Iterable[RedemptionEvent] = (),
) -> dict[str, dict[str, object]]:
    records = tuple(records)
    events = tuple(item for item in merge_redemptions(records, redemptions) if item.redeem_day <= as_of)
    result: dict[str, dict[str, object]] = {}
    for account in ("QMT1", "QMT2", "QMT3"):
        lots: list[IndiaLot] = []
        total_sold = 0
        account_records = [item for item in records if item.account == account and item.trade_day <= as_of]
        stream: list[tuple[tuple[datetime, int, int], str, object]] = []
        for record in account_records:
            if record.action in {"BUY", "SELL"}:
                stream.append((_event_order(record.trade_day, record.trade_dt, 0, record.row_number), "trade", record))
        for sequence, event in enumerate(item for item in events if item.account == account):
            stream.append((_event_order(event.redeem_day, event.event_dt, 1, sequence), "redeem", event))
        stream.sort(key=lambda item: item[0])
        total_redeemed = 0
        for _order, kind, payload in stream:
            if kind == "redeem":
                event = payload
                assert isinstance(event, RedemptionEvent)
                _consume(lots, event.qty, mature_only=True, as_of=event.redeem_day)
                total_redeemed += event.qty
                continue
            record = payload
            assert isinstance(record, IndiaTrade)
            if record.action == "BUY":
                lots.append(
                    IndiaLot(
                        lot_id=f"{account}:{record.trade_day}:{record.row_number}",
                        account=account,
                        buy_day=record.trade_day,
                        eligible_day=calendar.eligible_day(record.trade_day, holding_days),
                        qty=record.qty,
                        remaining_qty=record.qty,
                        cost_per_share=_record_cost(record) / Decimal(record.qty),
                        source_row=record.row_number,
                    )
                )
            else:
                _consume(lots, record.qty)
                total_sold += record.qty
        total_qty = sum(item.remaining_qty for item in lots)
        eligible_qty = sum(item.remaining_qty for item in lots if item.eligible_day <= as_of)
        result[account] = {
            "total_qty": total_qty,
            "eligible_qty": eligible_qty,
            "blocked_qty": total_qty - eligible_qty,
            "lot_count": sum(item.remaining_qty > 0 for item in lots),
            "last_trade_day": max((item.trade_day for item in account_records), default=None),
            "total_sold": total_sold,
            "reserved_qty": total_redeemed,
            "confidence": "ledger_only",
        }
    return result


def position_inventory_snapshot(
    position_snapshots: Iterable[PositionSnapshot],
    redemptions: Iterable[RedemptionEvent],
    calendar: TradingCalendar,
    as_of: date,
    *,
    holding_days: int = 3,
    basket_qty: int = 270_000,
) -> dict[str, dict[str, object]]:
    """Calculate conservative redeemable quantity from three dated closing snapshots."""
    snapshots = tuple(item for item in position_snapshots if item.day <= as_of)
    events = tuple(item for item in redemptions if item.redeem_day <= as_of)
    lookback_days = calendar.previous_sessions(as_of, holding_days)
    lookup = {(item.day, item.account): item for item in snapshots}
    result: dict[str, dict[str, object]] = {}
    for account in ("QMT1", "QMT2", "QMT3"):
        account_items = [item for item in snapshots if item.account == account]
        latest = max(account_items, key=lambda item: item.day, default=None)
        lookback_items = [lookup.get((day, account)) for day in lookback_days]
        complete = all(item is not None for item in lookback_items)
        quantities = tuple(item.total_qty if item is not None else None for item in lookback_items)
        snapshot_eligible = min(int(value) for value in quantities if value is not None) if complete else 0
        reserved = 0
        for event in events:
            if event.account != account:
                continue
            # Dated chicang files are end-of-day snapshots. A snapshot on or
            # after the redemption day is treated as the reconciliation point.
            reflected = any(item.day >= event.redeem_day for item in account_items)
            if not reflected:
                reserved += event.qty
        eligible = max(0, snapshot_eligible - reserved) if complete else 0
        latest_total = latest.total_qty if latest is not None else 0
        current_total = max(0, latest_total - reserved)
        warnings: list[str] = []
        if not complete:
            missing = [day.isoformat() for day, item in zip(lookback_days, lookback_items) if item is None]
            warnings.append("缺少持仓快照：" + ", ".join(missing))
        result[account] = {
            "total_qty": current_total,
            "eligible_qty": eligible,
            "blocked_qty": max(0, current_total - eligible),
            "lot_count": len(account_items),
            "last_trade_day": latest.day if latest is not None else None,
            "latest_available_qty": latest.available_qty if latest is not None else 0,
            "lookback_days": lookback_days,
            "lookback_qtys": quantities,
            "snapshot_eligible_qty": snapshot_eligible,
            "reserved_qty": reserved,
            "full_baskets": eligible // basket_qty,
            "executable_qty": eligible // basket_qty * basket_qty,
            "residual_qty": eligible % basket_qty,
            "confidence": "confirmed" if complete else "blocked",
            "warnings": tuple(warnings),
        }
    return result


def available_quantity(
    records: Iterable[IndiaTrade],
    account: str,
    as_of: date,
    calendar: TradingCalendar,
    *,
    holding_days: int = 3,
) -> int:
    snapshot = account_inventory_snapshot(records, calendar, as_of, holding_days=holding_days)
    return int(snapshot.get(account.upper(), {}).get("eligible_qty", 0))


def _build_settlement(
    event: RedemptionEvent,
    basket_qty: int,
    config: IndiaConfig,
    calendar: TradingCalendar,
    as_of_day: date,
) -> IndiaSettlement:
    expected_statement, expected_available = calendar.settlement_days(
        event.redeem_day,
        config.settlement_statement_days,
        config.settlement_available_days,
    )
    ratio = Decimal(basket_qty) / Decimal(event.qty)
    gross = event.gross_amount * ratio if event.gross_amount is not None else None
    fee = event.fee_amount * ratio if event.fee_amount is not None else None
    net = event.net_amount * ratio if event.net_amount is not None else None
    nav = event.nav_per_share
    source = "unknown"
    if net is not None:
        source = "statement_net" if event.source == "statement" else "manual_net"
    elif nav is not None:
        gross = Decimal(basket_qty) * nav
        fee = gross * config.redemption_fee_rate
        net = gross - fee
        source = "nav_estimate"
    elif gross is not None:
        fee = fee if fee is not None else gross * config.redemption_fee_rate
        net = gross - fee
        source = "gross_estimate"
    actual_available_day = None
    available_day_source = "unknown"
    if event.statement_day is not None:
        lag = max(1, config.settlement_available_days - config.settlement_statement_days)
        derived_available_day = calendar.offset(event.statement_day, lag)
        if as_of_day >= derived_available_day:
            actual_available_day = derived_available_day
            available_day_source = "derived_from_statement_day"
    return IndiaSettlement(
        expected_statement_day=expected_statement,
        expected_available_day=expected_available,
        actual_statement_day=event.statement_day,
        actual_available_day=actual_available_day,
        gross_amount=money(gross) if gross is not None else None,
        fee_amount=money(fee) if fee is not None else None,
        net_amount=money(net) if net is not None else None,
        nav_per_share=nav,
        amount_source=source,
        available_day_source=available_day_source,
    )


def _china_trade_day(dt: datetime) -> date:
    aware = dt.replace(tzinfo=NEW_YORK_TZ) if dt.tzinfo is None else dt
    return aware.astimezone(CHINA_TZ).date()


def _is_india_strategy_fill(fill: IbFill) -> bool:
    return fill.order_ref.strip().upper().startswith("INDIA_")


class _FillPool:
    def __init__(self, fills: Iterable[IbFill]) -> None:
        fills = tuple(fills)
        self.excluded = tuple(fill for fill in fills if not _is_india_strategy_fill(fill))
        self.items = [[fill, fill.abs_qty] for fill in fills if _is_india_strategy_fill(fill)]

    def take(self, symbol: str, side: str, target: int, day: date, max_days: int = 7) -> list[tuple[IbFill, int]]:
        candidates: list[tuple[int, datetime, int, list[object]]] = []
        for index, (fill, remaining) in enumerate(self.items):
            symbol_match = fill.symbol.startswith("NIFTY") if symbol == "NIFTY" else fill.symbol == symbol
            if remaining <= 0 or not symbol_match or fill.side != side:
                continue
            distance = abs((_china_trade_day(fill.dt) - day).days)
            if distance <= max_days:
                candidates.append((distance, fill.dt, index, [fill, remaining]))
        candidates.sort(key=lambda item: (item[0], item[1], item[2]))
        result: list[tuple[IbFill, int]] = []
        left = target
        for _distance, _dt, index, _copy in candidates:
            if left <= 0:
                break
            fill, remaining = self.items[index]
            used = min(left, int(remaining))
            self.items[index][1] = int(remaining) - used
            result.append((fill, used))
            left -= used
        return result


def _weighted_avg(items: list[tuple[IbFill, int]]) -> Decimal | None:
    qty = sum(quantity for _fill, quantity in items)
    if qty <= 0:
        return None
    return sum((fill.price * quantity for fill, quantity in items), Decimal("0")) / Decimal(qty)


def _commission(items: list[tuple[IbFill, int]]) -> Decimal:
    return sum(
        (fill.commission * Decimal(quantity) / Decimal(fill.abs_qty) for fill, quantity in items if fill.abs_qty),
        Decimal("0"),
    )


def _allocate_hedge(basket: IndiaBasket, pool: _FillPool, fx_rate: Decimal) -> None:
    if not basket.is_standard:
        basket.hedge_status = "not_applicable"
        return
    day = basket.redeem_day
    nifty_open = pool.take("NIFTY", "SELL", basket.nifty_target, day)
    nifty_close = pool.take("NIFTY", "BUY", basket.nifty_target, day)
    inda_open = pool.take("INDA", "SELL", basket.inda_target, day)
    inda_close = pool.take("INDA", "BUY", basket.inda_target, day)
    hedge = HedgeSummary(
        nifty_open_qty=sum(qty for _fill, qty in nifty_open),
        nifty_close_qty=sum(qty for _fill, qty in nifty_close),
        nifty_open_avg=_weighted_avg(nifty_open),
        nifty_close_avg=_weighted_avg(nifty_close),
        inda_open_qty=sum(qty for _fill, qty in inda_open),
        inda_close_qty=sum(qty for _fill, qty in inda_close),
        inda_open_avg=_weighted_avg(inda_open),
        inda_close_avg=_weighted_avg(inda_close),
        commissions_usd=_commission(nifty_open + nifty_close + inda_open + inda_close),
        fx_rate=fx_rate,
    )
    nifty_qty = min(hedge.nifty_open_qty, hedge.nifty_close_qty)
    if nifty_qty and hedge.nifty_open_avg is not None and hedge.nifty_close_avg is not None:
        hedge.nifty_pnl_usd = (hedge.nifty_open_avg - hedge.nifty_close_avg) * NIFTY_MULTIPLIER * nifty_qty
    inda_qty = min(hedge.inda_open_qty, hedge.inda_close_qty)
    if inda_qty and hedge.inda_open_avg is not None and hedge.inda_close_avg is not None:
        hedge.inda_pnl_usd = (hedge.inda_open_avg - hedge.inda_close_avg) * inda_qty
    hedge.pnl_cny = money(hedge.total_pnl_usd * fx_rate)
    if hedge.nifty_open_qty < basket.nifty_target or hedge.nifty_close_qty < basket.nifty_target:
        hedge.warnings.append("NIFTY 开平仓数量不足")
    if hedge.inda_open_qty < basket.inda_target or hedge.inda_close_qty < basket.inda_target:
        hedge.warnings.append("INDA 开平仓数量不足")
    basket.hedge = hedge
    if not hedge.warnings:
        basket.hedge_status = "fully_closed"
    else:
        basket.hedge_status = "mismatch"
        basket.warnings.extend(hedge.warnings)


def _event_baskets(
    event: RedemptionEvent,
    matches: tuple[LotMatch, ...],
    shortfall: int,
    config: IndiaConfig,
    calendar: TradingCalendar,
    as_of_day: date,
) -> list[IndiaBasket]:
    baskets: list[IndiaBasket] = []
    remaining = event.qty
    match_index = 0
    match_remaining = matches[0].qty if matches else 0
    sequence = 1
    while remaining > 0:
        basket_qty = min(config.basket_fund_qty, remaining)
        consumed: list[LotMatch] = []
        cost = Decimal("0")
        need = basket_qty
        while need > 0 and match_index < len(matches):
            item = matches[match_index]
            used = min(need, match_remaining)
            consumed.append(LotMatch(item.lot_id, item.buy_day, used, money(item.cost * Decimal(used) / Decimal(item.qty))))
            cost += consumed[-1].cost
            need -= used
            match_remaining -= used
            if match_remaining == 0:
                match_index += 1
                if match_index < len(matches):
                    match_remaining = matches[match_index].qty
        item_shortfall = max(0, need)
        basket = IndiaBasket(
            basket_id=f"{event.account}:{event.redeem_day:%Y%m%d}:{event.contract_no or event.event_id}:{sequence}",
            account=event.account,
            redeem_day=event.redeem_day,
            contract_no=event.contract_no,
            sequence=sequence,
            redeem_qty=basket_qty,
            domestic_cost=money(cost),
            domestic_matches=tuple(consumed),
            inventory_shortfall=item_shortfall if shortfall else 0,
            settlement=_build_settlement(event, basket_qty, config, calendar, as_of_day),
        )
        basket.settlement_status = basket.settlement.status
        if basket.inventory_shortfall:
            basket.data_quality = "blocked"
            basket.warnings.append(f"国内成熟库存缺口 {basket.inventory_shortfall:,} 份")
        if basket.settlement.amount_source in {"unknown"}:
            basket.data_quality = "estimated" if basket.data_quality == "complete" else basket.data_quality
            basket.warnings.append("尚无实际净赎回款或净值，国内收益暂不能确认")
        baskets.append(basket)
        remaining -= basket_qty
        sequence += 1
    return baskets


def calculate(
    records: Iterable[IndiaTrade],
    redemptions: Iterable[RedemptionEvent],
    config: IndiaConfig,
    *,
    fx_rate: Decimal | str | float = Decimal("0"),
    holidays: Iterable[date] = (),
    ib_fills: Iterable[IbFill] = (),
    position_snapshots: Iterable[PositionSnapshot] = (),
    as_of_day: date | None = None,
    calendar_years: Iterable[int] = (),
    fund_closed_days: Iterable[date] = (),
) -> IndiaCalculation:
    config.validate()
    records = tuple(sorted(records, key=lambda item: (item.event_dt, item.account, item.row_number)))
    all_events = merge_redemptions(records, redemptions)
    snapshot_day = as_of_day or max((item.redeem_day for item in all_events), default=date.today())
    events = tuple(item for item in all_events if item.redeem_day <= snapshot_day)
    calendar = TradingCalendar.official(
        extra_holidays=tuple(holidays),
        fund_closed_days=tuple(fund_closed_days),
        covered_years=tuple(calendar_years),
    )
    position_snapshots = tuple(position_snapshots)
    if position_snapshots:
        snapshots = position_inventory_snapshot(
            position_snapshots,
            events,
            calendar,
            snapshot_day,
            holding_days=config.redemption_holding_days,
            basket_qty=config.basket_fund_qty,
        )
    else:
        snapshots = account_inventory_snapshot(
            records,
            calendar,
            snapshot_day,
            holding_days=config.redemption_holding_days,
            redemptions=events,
        )
    baskets: list[IndiaBasket] = []
    warnings: list[str] = []
    by_account = {
        account: [item for item in records if item.account == account and item.trade_day <= snapshot_day]
        for account in ("QMT1", "QMT2", "QMT3")
    }
    for account in ("QMT1", "QMT2", "QMT3"):
        lots: list[IndiaLot] = []
        sequence = 0
        stream: list[tuple[tuple[datetime, int, int], str, object]] = []
        for record in by_account[account]:
            if record.action in {"BUY", "SELL"}:
                stream.append((_event_order(record.trade_day, record.trade_dt, 0, record.row_number), "trade", record))
        for event in events:
            if event.account == account:
                stream.append((_event_order(event.redeem_day, event.event_dt, 1, sequence), "redeem", event))
                sequence += 1
        stream.sort(key=lambda item: item[0])
        for _order, kind, payload in stream:
            if kind == "trade":
                record = payload
                assert isinstance(record, IndiaTrade)
                if record.action == "BUY":
                    lots.append(
                        IndiaLot(
                            lot_id=f"{account}:{record.trade_day}:{record.row_number}",
                            account=account,
                            buy_day=record.trade_day,
                            eligible_day=calendar.eligible_day(record.trade_day, config.redemption_holding_days),
                            qty=record.qty,
                            remaining_qty=record.qty,
                            cost_per_share=_record_cost(record) / Decimal(record.qty),
                            source_row=record.row_number,
                        )
                    )
                else:
                    _consume(lots, record.qty)
                continue
            event = payload
            assert isinstance(event, RedemptionEvent)
            matches, shortfall = _consume(lots, event.qty, mature_only=True, as_of=event.redeem_day)
            baskets.extend(_event_baskets(event, matches, shortfall, config, calendar, snapshot_day))
    pool = _FillPool(ib_fills)
    rate = Decimal(str(fx_rate))
    for basket in baskets:
        _allocate_hedge(basket, pool, rate)
    if pool.excluded:
        warnings.append(
            f"已隔离 {len(pool.excluded)} 笔缺少 INDIA_ orderRef 的 NIFTY/INDA 成交，未计入篮子收益"
        )
    for account, item in snapshots.items():
        for warning in item.get("warnings", ()):
            warnings.append(f"{account}: {warning}")
    if not config.redemption_fee_confirmed:
        warnings.append("赎回费率 0.464% 仍为可配置实盘口径，待首张 164824 交割单确认")
    if not baskets:
        warnings.append("尚未登记 164824 赎回事件；当前只显示三账户可赎数量")
    return IndiaCalculation(tuple(baskets), records, events, snapshots, tuple(warnings))


def calculate_from_paths(
    paths: dict[str, Path | str | None],
    config: IndiaConfig,
    *,
    redemptions: Iterable[RedemptionEvent] = (),
    ib_path: Path | str | None = None,
    fx_rate: Decimal | str | float = Decimal("0"),
    holidays: Iterable[date] = (),
    as_of_day: date | None = None,
    qmt_time_root: Path | str | None = None,
    position_snapshots: Iterable[PositionSnapshot] = (),
    calendar_years: Iterable[int] = (),
    fund_closed_days: Iterable[date] = (),
) -> IndiaCalculation:
    records = load_qmt_accounts(paths, config.fund_code, qmt_time_root)
    fills = load_ib_india_fills(ib_path)
    return calculate(
        records,
        redemptions,
        config,
        fx_rate=fx_rate,
        holidays=holidays,
        ib_fills=fills,
        position_snapshots=position_snapshots,
        as_of_day=as_of_day,
        calendar_years=calendar_years,
        fund_closed_days=fund_closed_days,
    )
