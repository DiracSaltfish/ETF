from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
from enum import Enum
from zoneinfo import ZoneInfo


NEW_YORK = ZoneInfo("America/New_York")
DEFAULT_TOTAL_QTY = 990
SLICE_QTY = 200
TRIGGER_TIMES_BY_BASKET_COUNT = {
    1: (
        time(15, 58, 45),
        time(15, 58, 52),
        time(15, 59, 0),
        time(15, 59, 7),
        time(15, 59, 15),
    ),
    2: (
        time(15, 58, 45),
        time(15, 58, 48),
        time(15, 58, 52),
        time(15, 58, 55),
        time(15, 58, 58),
        time(15, 59, 2),
        time(15, 59, 5),
        time(15, 59, 8),
        time(15, 59, 12),
        time(15, 59, 15),
    ),
}
TRIGGER_TIMES = TRIGGER_TIMES_BY_BASKET_COUNT[1]
FOUR_ORDER_TRIGGER_TIME_PLANS = {
    "balanced": (
        time(15, 59, 0),
        time(15, 59, 15),
        time(15, 59, 30),
        time(15, 59, 45),
    ),
    "conservative": (
        time(15, 58, 30),
        time(15, 58, 45),
        time(15, 59, 0),
        time(15, 59, 15),
    ),
}
# 保留旧名称，兼容现有调用方。
CREATION_TRIGGER_TIME_PLANS = FOUR_ORDER_TRIGGER_TIME_PLANS


class XopCloseIntent(str, Enum):
    REDEMPTION_BUY = "redemption_buy"
    CREATION_SELL = "creation_sell"

    @property
    def action(self) -> str:
        return "BUY" if self is XopCloseIntent.REDEMPTION_BUY else "SELL"

    @property
    def business_label(self) -> str:
        if self is XopCloseIntent.REDEMPTION_BUY:
            return "赎回国内-买入XOP"
        return "申购国内-卖出XOP"

    @property
    def order_ref_prefix(self) -> str:
        if self is XopCloseIntent.REDEMPTION_BUY:
            return "XOP_REDEEM_CLOSE_"
        return "XOP_CREATE_CLOSE_"


SUPPORTED_ORDER_REF_PREFIXES = tuple(item.order_ref_prefix for item in XopCloseIntent)


@dataclass(frozen=True)
class XopCloseOrderSpec:
    sequence: int
    trade_date: date
    trigger_time: time
    quantity: int
    intent: XopCloseIntent = XopCloseIntent.REDEMPTION_BUY
    account: str = ""
    symbol: str = "XOP"
    order_type: str = "MKT"
    tif: str = "DAY"
    outside_rth: bool = False
    conditions_ignore_rth: bool = False
    conditions_cancel_order: bool = False
    transmit: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "intent", XopCloseIntent(self.intent))
        object.__setattr__(self, "account", str(self.account or "").strip())
        if self.account:
            _safe_account_token(self.account)

    @property
    def trigger_datetime(self) -> datetime:
        return datetime.combine(self.trade_date, self.trigger_time, NEW_YORK)

    @property
    def condition_time(self) -> str:
        return f"{self.trade_date:%Y%m%d} {self.trigger_time:%H:%M:%S} US/Eastern"

    @property
    def action(self) -> str:
        return self.intent.action

    @property
    def order_ref(self) -> str:
        account_suffix = (
            f"_{_safe_account_token(self.account)}"
            if self.account and self.intent is XopCloseIntent.CREATION_SELL
            else ""
        )
        return (
            f"{self.intent.order_ref_prefix}{self.trade_date:%Y%m%d}_"
            f"{self.trigger_time:%H%M%S}_{self.quantity}{account_suffix}"
        )


def _safe_account_token(account: str) -> str:
    token = "".join(character for character in str(account).upper() if character.isalnum())
    if not token:
        raise ValueError("IB账户编号无效")
    return token


def is_xop_close_order_ref(order_ref: str) -> bool:
    return str(order_ref or "").startswith(SUPPORTED_ORDER_REF_PREFIXES)


def parse_trade_date(value: str) -> date:
    text = value.strip()
    if len(text) != 8 or not text.isdigit():
        raise ValueError("条件单日期必须是8位 YYYYMMDD，例如 20260706")
    return datetime.strptime(text, "%Y%m%d").date()


def trigger_times_for_basket_count(basket_count: int) -> tuple[time, ...]:
    try:
        return TRIGGER_TIMES_BY_BASKET_COUNT[basket_count]
    except KeyError as exc:
        raise ValueError("平仓篮子数只支持1张或2张") from exc


def minimum_total_qty_for_basket_count(basket_count: int) -> int:
    trigger_times = trigger_times_for_basket_count(basket_count)
    return SLICE_QTY * (len(trigger_times) - 1)


def four_order_trigger_times(plan_key: str = "balanced") -> tuple[time, ...]:
    try:
        return FOUR_ORDER_TRIGGER_TIME_PLANS[plan_key]
    except KeyError as exc:
        raise ValueError(f"不支持的四笔时间方案：{plan_key}") from exc


def creation_trigger_times(plan_key: str = "balanced") -> tuple[time, ...]:
    return four_order_trigger_times(plan_key)


def balanced_quantities(total_qty: int, order_count: int) -> tuple[int, ...]:
    if order_count <= 0:
        raise ValueError("订单数量必须大于0")
    if total_qty < order_count:
        raise ValueError(f"目标数量至少为{order_count}股，确保每张订单数量大于0")
    base, remainder = divmod(total_qty, order_count)
    return tuple(base + (1 if index < remainder else 0) for index in range(order_count))


def generate_order_specs(
    trade_date: date,
    total_qty: int = DEFAULT_TOTAL_QTY,
    *,
    basket_count: int = 1,
    intent: XopCloseIntent = XopCloseIntent.REDEMPTION_BUY,
    trigger_times: tuple[time, ...] | None = None,
    account: str = "",
) -> tuple[XopCloseOrderSpec, ...]:
    if trade_date.weekday() >= 5:
        raise ValueError("条件单日期必须是美国常规交易日（周一至周五）")
    try:
        close_intent = XopCloseIntent(intent)
    except ValueError as exc:
        raise ValueError(f"不支持的XOP平仓业务类型：{intent}") from exc
    if basket_count <= 0:
        raise ValueError("篮子数必须大于0")
    if account:
        _safe_account_token(account)
    if close_intent is XopCloseIntent.REDEMPTION_BUY and trigger_times is None:
        resolved_trigger_times = trigger_times_for_basket_count(basket_count)
        minimum_total_qty = minimum_total_qty_for_basket_count(basket_count)
        order_count = len(resolved_trigger_times)
        if total_qty <= minimum_total_qty:
            raise ValueError(f"目标数量必须大于{minimum_total_qty}股，确保第{order_count}张订单数量大于0")
        quantities = (SLICE_QTY,) * (order_count - 1) + (total_qty - minimum_total_qty,)
    else:
        resolved_trigger_times = (
            four_order_trigger_times()
            if trigger_times is None
            else tuple(trigger_times)
        )
        if resolved_trigger_times not in tuple(FOUR_ORDER_TRIGGER_TIME_PLANS.values()):
            raise ValueError("四笔均分只支持预设的4个美东触发时间")
        order_count = len(resolved_trigger_times)
        quantities = balanced_quantities(total_qty, order_count)
    specs = tuple(
        XopCloseOrderSpec(
            index,
            trade_date,
            trigger_time,
            quantity,
            intent=close_intent,
            account=account,
        )
        for index, (trigger_time, quantity) in enumerate(
            zip(resolved_trigger_times, quantities), start=1
        )
    )
    validate_order_specs(
        specs,
        total_qty,
        expected_count=order_count,
        expected_trigger_times=resolved_trigger_times,
    )
    return specs


def validate_order_specs(
    specs: tuple[XopCloseOrderSpec, ...],
    total_qty: int,
    *,
    expected_count: int = 5,
    expected_trigger_times: tuple[time, ...] | None = None,
) -> None:
    if len(specs) != expected_count:
        raise ValueError(f"必须生成{expected_count}张订单")
    if sum(item.quantity for item in specs) != total_qty:
        raise ValueError(f"{expected_count}张订单的数量合计不等于目标数量")
    if any(item.quantity <= 0 for item in specs):
        raise ValueError("每张订单数量必须大于0")
    if len({item.order_ref for item in specs}) != len(specs):
        raise ValueError("orderRef重复")
    intents = {item.intent for item in specs}
    if len(intents) != 1:
        raise ValueError("同一批订单不能混合赎回买入和申购卖出")
    intent = next(iter(intents))
    accounts = {item.account for item in specs}
    if len(accounts) != 1:
        raise ValueError("同一批订单必须使用同一个IB账户")
    actual_trigger_times = tuple(item.trigger_time for item in specs)
    if len(set(actual_trigger_times)) != len(actual_trigger_times):
        raise ValueError("触发时间不能重复")
    if actual_trigger_times != tuple(sorted(actual_trigger_times)):
        raise ValueError("触发时间必须严格递增")
    if expected_trigger_times is not None and actual_trigger_times != expected_trigger_times:
        raise ValueError("触发时间与选定时间方案不一致")
    if intent is XopCloseIntent.REDEMPTION_BUY:
        allowed_schedules = (
            *TRIGGER_TIMES_BY_BASKET_COUNT.values(),
            *FOUR_ORDER_TRIGGER_TIME_PLANS.values(),
        )
    else:
        allowed_schedules = tuple(FOUR_ORDER_TRIGGER_TIME_PLANS.values())
    if actual_trigger_times not in allowed_schedules:
        raise ValueError("触发时间不属于当前业务允许的预设方案")
    for item in specs:
        if intent is XopCloseIntent.REDEMPTION_BUY:
            if not (time(15, 58, 30) <= item.trigger_time <= time(15, 59, 45)):
                raise ValueError("赎回买入触发时间必须位于15:58:30至15:59:45")
        elif not (time(15, 58, 30) <= item.trigger_time <= time(15, 59, 45)):
            raise ValueError("申购卖出触发时间必须位于15:58:30至15:59:45")
        if (
            item.action != intent.action
            or item.order_type != "MKT"
            or item.tif != "DAY"
            or item.outside_rth
            or item.conditions_ignore_rth
            or item.conditions_cancel_order
            or not item.transmit
        ):
            raise ValueError(f"订单{item.sequence}参数不符合固定安全模板")


def validate_future_trigger(spec: XopCloseOrderSpec, *, now: datetime | None = None) -> None:
    current = now or datetime.now(NEW_YORK)
    if current.tzinfo is None:
        current = current.replace(tzinfo=NEW_YORK)
    else:
        current = current.astimezone(NEW_YORK)
    if spec.trigger_datetime <= current:
        raise ValueError(
            f"{spec.order_ref} 的触发时间 {spec.trigger_datetime:%Y-%m-%d %H:%M:%S %Z} 已经过期，禁止发送"
        )


def build_xop_contract():
    from ib_insync import Stock

    contract = Stock("XOP", "SMART", "USD", primaryExchange="ARCA")
    contract.conId = 413951498
    contract.tradingClass = "XOP"
    return contract


def build_ib_order(spec: XopCloseOrderSpec):
    from ib_insync import MarketOrder, TimeCondition

    order = MarketOrder(
        spec.action,
        spec.quantity,
        tif="DAY",
        outsideRth=False,
        transmit=True,
        orderRef=spec.order_ref,
    )
    if spec.account:
        order.account = spec.account
    order.conditions = [TimeCondition(isMore=True, time=spec.condition_time)]
    order.conditionsIgnoreRth = False
    order.conditionsCancelOrder = False
    return order
