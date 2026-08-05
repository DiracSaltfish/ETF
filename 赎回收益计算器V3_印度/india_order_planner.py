from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable

from india_calendar import NEW_YORK_TZ, beijing_display, zoned_datetime
from india_config import IndiaConfig
from india_models import IndiaOrderSpec
from nifty_contract_resolver import resolve_nifty_contract


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


def build_ib_order(spec: IndiaOrderSpec):
    from ib_insync import MarketOrder, TimeCondition

    order = MarketOrder(
        spec.action,
        spec.quantity,
        tif="DAY",
        outsideRth=False,
        transmit=True,
        orderRef=spec.order_ref,
    )
    order.conditions = [TimeCondition(isMore=True, time=spec.trigger_dt.strftime("%Y%m%d %H:%M:%S US/Eastern"))]
    order.conditionsIgnoreRth = False
    order.conditionsCancelOrder = False
    return order


def validate_preview_only(config: IndiaConfig, specs: Iterable[IndiaOrderSpec]) -> None:
    items = tuple(specs)
    refs = [item.order_ref for item in items]
    if len(refs) != len(set(refs)):
        raise ValueError("订单 orderRef 重复")
    if config.live_enabled:
        raise ValueError("实盘开关已打开；当前版本要求先关闭 live_enabled 后仅生成预览")
    for item in items:
        if item.quantity <= 0:
            raise ValueError("订单数量必须大于 0")
        if item.action not in {"BUY", "SELL"}:
            raise ValueError("订单方向无效")
