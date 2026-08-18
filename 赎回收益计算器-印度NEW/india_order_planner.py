from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from india_calendar import NEW_YORK_TZ, beijing_display, zoned_datetime
from india_config import IndiaConfig
from india_models import IndiaOrderSpec
from nifty_contract_resolver import resolve_nifty_contract


NYSE_HOLIDAYS_BY_YEAR = {
    2026: frozenset(
        {
            date(2026, 1, 1),
            date(2026, 1, 19),
            date(2026, 2, 16),
            date(2026, 4, 3),
            date(2026, 5, 25),
            date(2026, 6, 19),
            date(2026, 7, 3),
            date(2026, 9, 7),
            date(2026, 11, 26),
            date(2026, 12, 25),
        }
    )
}

NYSE_EARLY_CLOSES_BY_YEAR = {
    2026: frozenset({date(2026, 11, 27), date(2026, 12, 24)})
}


def _quantity_for_actual_open(actual_qty: int, config: IndiaConfig) -> tuple[int, int]:
    if actual_qty <= 0:
        raise ValueError("实际 INDA 开仓数量必须大于 0")
    first = int(
        (Decimal(actual_qty) * Decimal(config.inda_first_close_shares) / Decimal(config.inda_shares_per_basket))
        .to_integral_value(rounding=ROUND_HALF_UP)
    )
    first = max(0, min(actual_qty, first))
    return first, actual_qty - first


def split_inda_close_qty(actual_qty: int, config: IndiaConfig) -> tuple[int, int]:
    first, second = _quantity_for_actual_open(actual_qty, config)
    if actual_qty == config.inda_shares_per_basket:
        assert (first, second) == (config.inda_first_close_shares, config.inda_second_close_shares)
    return first, second


def build_swap_plan(
    trade_day: date,
    basket_count: int,
    config: IndiaConfig,
    *,
    nifty_override: str | None = None,
    contract_details: dict[str, object] | None = None,
) -> tuple[IndiaOrderSpec, ...]:
    if basket_count <= 0:
        raise ValueError("换仓篮子数必须大于 0")
    contract = resolve_nifty_contract(
        trade_day,
        roll_weekday=config.nifty_roll_weekday,
        override=nifty_override,
        contract_details=contract_details,
    )
    trigger = zoned_datetime(trade_day, config.swap_time_et, NEW_YORK_TZ)
    nifty_qty = basket_count * config.nifty_contracts_per_basket
    inda_qty = basket_count * config.inda_shares_per_basket
    return (
        IndiaOrderSpec(
            order_ref=f"INDIA_SWAP_NIFTY_BUY_{trade_day:%Y%m%d}_{nifty_qty}",
            sequence=1,
            trade_day=trade_day,
            symbol=contract.local_symbol,
            action="BUY",
            quantity=nifty_qty,
            trigger_dt=trigger,
            purpose="先买入 NIFTY 平掉既有空头",
            contract_month=contract.expiry_month,
            live_allowed=config.live_enabled,
        ),
        IndiaOrderSpec(
            order_ref=f"INDIA_SWAP_INDA_SELL_{trade_day:%Y%m%d}_{inda_qty}",
            sequence=2,
            trade_day=trade_day,
            symbol="INDA",
            action="SELL",
            quantity=inda_qty,
            trigger_dt=trigger,
            purpose="NIFTY 成交后卖空 INDA 建立新对冲",
            live_allowed=config.live_enabled,
        ),
    )


def build_inda_close_plan(
    trade_day: date,
    actual_open_qty: int,
    config: IndiaConfig,
) -> tuple[IndiaOrderSpec, ...]:
    first, second = split_inda_close_qty(actual_open_qty, config)
    if first <= 0 or second <= 0:
        raise ValueError("INDA 两段平仓数量必须均大于 0")
    specs = (
        IndiaOrderSpec(
            order_ref=f"INDIA_INDA_CLOSE_1130_{trade_day:%Y%m%d}_{first}",
            sequence=1,
            trade_day=trade_day,
            symbol="INDA",
            action="BUY",
            quantity=first,
            trigger_dt=zoned_datetime(trade_day, config.inda_first_close_time_et, NEW_YORK_TZ),
            purpose="美东 11:30 第一段平仓",
            live_allowed=config.live_enabled,
        ),
        IndiaOrderSpec(
            order_ref=f"INDIA_INDA_CLOSE_1559_{trade_day:%Y%m%d}_{second}",
            sequence=2,
            trade_day=trade_day,
            symbol="INDA",
            action="BUY",
            quantity=second,
            trigger_dt=zoned_datetime(trade_day, config.inda_second_close_time_et, NEW_YORK_TZ),
            purpose="美东 15:59 第二段平仓",
            live_allowed=config.live_enabled,
        ),
    )
    if sum(item.quantity for item in specs) != actual_open_qty:
        raise ValueError("INDA 平仓计划数量不守恒")
    return specs


def plan_display_rows(specs: Iterable[IndiaOrderSpec]) -> list[list[str]]:
    return [
        [
            item.sequence,
            item.symbol,
            item.action,
            f"{item.quantity:,}",
            item.trigger_time_et,
            beijing_display(item.trigger_dt),
            item.contract_month or "--",
            item.purpose,
            item.order_ref,
        ]
        for item in specs
    ]


def build_ib_contract(spec: IndiaOrderSpec):
    from ib_insync import Future, Stock

    if spec.symbol.startswith("NIFTY"):
        contract = Future("NIFTY", spec.contract_month, "CME", currency="USD", multiplier=2)
        contract.localSymbol = spec.symbol
        return contract
    return Stock("INDA", "SMART", "USD", primaryExchange="ARCA")


def validate_qualified_contract(spec: IndiaOrderSpec, contract: object) -> None:
    con_id = int(getattr(contract, "conId", 0) or 0)
    if con_id <= 0:
        raise ValueError(f"TWS未返回 {spec.symbol} 的有效 conId")
    symbol = str(getattr(contract, "symbol", "") or "").upper()
    local_symbol = str(getattr(contract, "localSymbol", "") or "").upper()
    sec_type = str(getattr(contract, "secType", "") or "").upper()
    currency = str(getattr(contract, "currency", "") or "").upper()
    if spec.symbol == "INDA":
        if symbol != "INDA" or sec_type != "STK" or currency != "USD":
            raise ValueError("TWS返回的INDA合约身份不符，订单未发送")
        return
    if sec_type != "FUT" or local_symbol != spec.symbol.upper() or currency != "USD":
        raise ValueError(f"TWS返回的NIFTY合约 {local_symbol or symbol} 与预览 {spec.symbol} 不符")
    expiry = str(getattr(contract, "lastTradeDateOrContractMonth", "") or "")[:6]
    if spec.contract_month and expiry != spec.contract_month:
        raise ValueError(f"TWS返回NIFTY合约月 {expiry or '--'}，预览为 {spec.contract_month}")
    multiplier = str(getattr(contract, "multiplier", "") or "")
    if multiplier not in {"2", "2.0"}:
        raise ValueError(f"TWS返回NIFTY乘数 {multiplier or '--'}，预期为2")


def build_ib_order(
    spec: IndiaOrderSpec,
    *,
    account: str = "",
    transmit: bool = True,
    parent_id: int = 0,
    include_time_condition: bool = True,
):
    from ib_insync import MarketOrder, TimeCondition

    order = MarketOrder(
        spec.action,
        spec.quantity,
        tif="DAY",
        outsideRth=False,
        transmit=transmit,
        orderRef=spec.order_ref,
    )
    if account:
        order.account = account
    if parent_id:
        order.parentId = parent_id
    order.conditions = (
        [TimeCondition(isMore=True, time=spec.trigger_dt.strftime("%Y%m%d %H:%M:%S US/Eastern"))]
        if include_time_condition
        else []
    )
    order.conditionsIgnoreRth = False
    order.conditionsCancelOrder = False
    return order


def validate_preview_only(config: IndiaConfig, specs: Iterable[IndiaOrderSpec]) -> None:
    items = tuple(specs)
    refs = [item.order_ref for item in items]
    if len(refs) != len(set(refs)):
        raise ValueError("订单 orderRef 重复")
    for item in items:
        if item.quantity <= 0:
            raise ValueError("订单数量必须大于 0")
        if item.action not in {"BUY", "SELL"}:
            raise ValueError("订单方向无效")


def validate_live_plan(
    config: IndiaConfig,
    specs: Iterable[IndiaOrderSpec],
    *,
    now: datetime | None = None,
) -> tuple[IndiaOrderSpec, ...]:
    items = tuple(specs)
    validate_preview_only(config, items)
    if not items:
        raise ValueError("请先生成订单预览")
    if not config.live_enabled:
        raise ValueError("实盘总开关未开启；请先在数据源设置中开启")
    if not all(item.live_allowed for item in items):
        raise ValueError("预览生成时实盘开关尚未开启，请重新生成预览")
    for item in items:
        current = now or datetime.now(item.trigger_dt.tzinfo)
        if current.tzinfo is None:
            current = current.replace(tzinfo=item.trigger_dt.tzinfo)
        current = current.astimezone(item.trigger_dt.tzinfo)
        if item.trade_day != current.date():
            raise ValueError(
                f"{item.order_ref} 不是美东当日订单；DAY条件单只允许在交易日当天发送"
            )
        if item.trade_day.weekday() >= 5:
            raise ValueError("周末不允许发送INDA/NIFTY换仓或平仓条件单")
        holidays = NYSE_HOLIDAYS_BY_YEAR.get(item.trade_day.year)
        if holidays is None:
            raise ValueError(f"缺少 {item.trade_day.year} 年NYSE官方交易日历，实盘发送已阻断")
        if item.trade_day in holidays:
            raise ValueError(f"{item.trade_day} 是美国证券市场休市日，订单未发送")
        early_closes = NYSE_EARLY_CLOSES_BY_YEAR.get(item.trade_day.year, frozenset())
        if item.trade_day in early_closes and (item.trigger_dt.hour, item.trigger_dt.minute) >= (13, 0):
            raise ValueError(f"{item.trade_day} 13:00美东提前收盘，当前触发时间不可用")
        if item.trigger_dt <= current:
            raise ValueError(f"{item.order_ref} 的触发时间已经过去，请重新选择交易日")
        if not item.order_ref.startswith("INDIA_"):
            raise ValueError("只允许发送 INDIA_ 策略命名空间内的订单")
    return items


def is_swap_batch(specs: Iterable[IndiaOrderSpec]) -> bool:
    items = tuple(specs)
    return (
        len(items) == 2
        and items[0].order_ref.startswith("INDIA_SWAP_NIFTY_BUY_")
        and items[1].order_ref.startswith("INDIA_SWAP_INDA_SELL_")
        and items[0].action == "BUY"
        and items[1].action == "SELL"
    )
