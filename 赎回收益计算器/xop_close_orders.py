from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time
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


@dataclass(frozen=True)
class XopCloseOrderSpec:
    sequence: int
    trade_date: date
    trigger_time: time
    quantity: int
    symbol: str = "XOP"
    action: str = "BUY"
    order_type: str = "MKT"
    tif: str = "DAY"
    outside_rth: bool = False
    conditions_ignore_rth: bool = False
    conditions_cancel_order: bool = False
    transmit: bool = True

    @property
    def trigger_datetime(self) -> datetime:
        return datetime.combine(self.trade_date, self.trigger_time, NEW_YORK)

    @property
    def condition_time(self) -> str:
        return f"{self.trade_date:%Y%m%d} {self.trigger_time:%H:%M:%S} US/Eastern"

    @property
    def order_ref(self) -> str:
        return (
            f"XOP_REDEEM_CLOSE_{self.trade_date:%Y%m%d}_"
            f"{self.trigger_time:%H%M%S}_{self.quantity}"
        )


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


def generate_order_specs(
    trade_date: date,
    total_qty: int = DEFAULT_TOTAL_QTY,
    *,
    basket_count: int = 1,
) -> tuple[XopCloseOrderSpec, ...]:
    if trade_date.weekday() >= 5:
        raise ValueError("条件单日期必须是美国常规交易日（周一至周五）")
    trigger_times = trigger_times_for_basket_count(basket_count)
    minimum_total_qty = minimum_total_qty_for_basket_count(basket_count)
    order_count = len(trigger_times)
    if total_qty <= minimum_total_qty:
        raise ValueError(f"目标数量必须大于{minimum_total_qty}股，确保第{order_count}张订单数量大于0")
    quantities = (SLICE_QTY,) * (order_count - 1) + (total_qty - minimum_total_qty,)
    specs = tuple(
        XopCloseOrderSpec(index, trade_date, trigger_time, quantity)
        for index, (trigger_time, quantity) in enumerate(zip(trigger_times, quantities), start=1)
    )
    validate_order_specs(specs, total_qty, expected_count=order_count)
    return specs


def validate_order_specs(
    specs: tuple[XopCloseOrderSpec, ...],
    total_qty: int,
    *,
    expected_count: int = 5,
) -> None:
    if len(specs) != expected_count:
        raise ValueError(f"必须生成{expected_count}张订单")
    if sum(item.quantity for item in specs) != total_qty:
        raise ValueError(f"{expected_count}张订单的数量合计不等于目标数量")
    if any(item.quantity <= 0 for item in specs):
        raise ValueError("每张订单数量必须大于0")
    if len({item.order_ref for item in specs}) != len(specs):
        raise ValueError("orderRef重复")
    for item in specs:
        if not (time(15, 58, 45) <= item.trigger_time <= time(15, 59, 15)):
            raise ValueError("所有触发时间必须位于15:58:45（含）至15:59:15（含），为收盘前补单保留缓冲")
        if (
            item.action != "BUY"
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
        "BUY",
        spec.quantity,
        tif="DAY",
        outsideRth=False,
        transmit=True,
        orderRef=spec.order_ref,
    )
    order.conditions = [TimeCondition(isMore=True, time=spec.condition_time)]
    order.conditionsIgnoreRth = False
    order.conditionsCancelOrder = False
    return order
