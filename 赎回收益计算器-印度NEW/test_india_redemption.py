from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from india_calendar import CalendarCoverageError, TradingCalendar
from india_config import IndiaConfig
from india_engine import account_inventory_snapshot, calculate, position_inventory_snapshot
from india_models import IbFill, IndiaTrade, RedemptionEvent
from india_sources import load_ib_india_fills, load_position_snapshots, load_redemption_statement


def _trade(account: str, day: date, action: str, qty: int, price: str = "1") -> IndiaTrade:
    return IndiaTrade(
        account=account,
        row_number=1,
        trade_day=day,
        action=action,  # type: ignore[arg-type]
        qty=qty,
        price=Decimal(price),
        amount=Decimal(price) * qty,
        trade_dt=datetime.combine(day, datetime.min.time()),
    )


def test_buy_must_mature_for_three_trading_days() -> None:
    calendar = TradingCalendar()
    monday = date(2026, 8, 3)
    records = (_trade("QMT1", monday, "BUY", 100),)
    assert calendar.eligible_day(monday, 3) == date(2026, 8, 6)
    assert account_inventory_snapshot(records, calendar, date(2026, 8, 5))["QMT1"]["eligible_qty"] == 0
    assert account_inventory_snapshot(records, calendar, date(2026, 8, 6))["QMT1"]["eligible_qty"] == 100


def test_redemption_is_split_into_standard_baskets_and_settles_t5_t6() -> None:
    buy_day = date(2026, 7, 1)
    redeem_day = date(2026, 7, 6)
    records = (_trade("QMT1", buy_day, "BUY", 540_000, "1"),)
    event = RedemptionEvent(
        event_id="manual:test",
        account="QMT1",
        redeem_day=redeem_day,
        qty=540_000,
        source="manual",
        net_amount=Decimal("550000"),
    )
    result = calculate(records, (event,), IndiaConfig(), fx_rate=Decimal("6.8"))
    assert len(result.baskets) == 2
    assert all(item.redeem_qty == 270_000 for item in result.baskets)
    assert result.baskets[0].settlement.expected_statement_day == date(2026, 7, 13)
    assert result.baskets[0].settlement.expected_available_day == date(2026, 7, 14)
    assert result.baskets[0].domestic_cost == Decimal("270000.00")
    assert result.baskets[0].domestic_pnl == Decimal("5000.00")


def test_statement_net_amount_is_not_charged_fee_twice() -> None:
    records = (_trade("QMT2", date(2026, 6, 1), "BUY", 270_000, "2"),)
    event = RedemptionEvent(
        event_id="statement:test",
        account="QMT2",
        redeem_day=date(2026, 6, 8),
        qty=270_000,
        source="statement",
        net_amount=Decimal("500000"),
        fee_amount=Decimal("1000"),
    )
    result = calculate(records, (event,), IndiaConfig(), fx_rate=Decimal("6.8"))
    settlement = result.baskets[0].settlement
    assert settlement.amount_source == "statement_net"
    assert settlement.net_amount == Decimal("500000.00")
    assert result.baskets[0].domestic_pnl == Decimal("-40000.00")


def test_manual_redemption_is_deducted_from_ledger_snapshot() -> None:
    records = (_trade("QMT1", date(2026, 6, 1), "BUY", 270_000, "1"),)
    event = RedemptionEvent("manual:reserve-ledger", "QMT1", date(2026, 6, 8), 270_000)
    result = calculate(records, (event,), IndiaConfig(), as_of_day=date(2026, 6, 8))
    assert result.account_snapshots["QMT1"]["total_qty"] == 0
    assert result.account_snapshots["QMT1"]["eligible_qty"] == 0


def test_hedge_uses_nifty_and_inda_actual_closed_quantity() -> None:
    records = (_trade("QMT1", date(2026, 6, 1), "BUY", 270_000, "2"),)
    event = RedemptionEvent(
        event_id="manual:hedge",
        account="QMT1",
        redeem_day=date(2026, 6, 8),
        qty=270_000,
        source="manual",
        net_amount=Decimal("540000"),
    )
    fills = (
        IbFill("nifty-open", "NIFTYM26", "期货", datetime(2026, 6, 8, 10, 0), -1, Decimal("100"), order_ref="INDIA_NIFTY_OPEN"),
        IbFill("nifty-close", "NIFTYM26", "期货", datetime(2026, 6, 8, 11, 0), 1, Decimal("90"), order_ref="INDIA_SWAP_NIFTY_BUY"),
        IbFill("inda-open", "INDA", "股票", datetime(2026, 6, 8, 10, 0), -970, Decimal("50"), order_ref="INDIA_SWAP_INDA_SELL"),
        IbFill("inda-close", "INDA", "股票", datetime(2026, 6, 8, 11, 0), 970, Decimal("49"), order_ref="INDIA_INDA_CLOSE"),
    )
    result = calculate(records, (event,), IndiaConfig(), fx_rate=Decimal("6.8"), ib_fills=fills)
    basket = result.baskets[0]
    assert basket.hedge_status == "fully_closed"
    assert basket.hedge.nifty_open_qty == basket.hedge.nifty_close_qty == 1
    assert basket.hedge.inda_open_qty == basket.hedge.inda_close_qty == 970
    assert basket.hedge.pnl_cny == Decimal("6732.00")


def _write_position(path: Path, qty: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "code,name,volume,available,cost_price,current_price,profit,profit_rate,market_value\n"
        f"164824.SZ,印度基金LOF,{qty},{qty},1,1,0,0,{qty}\n",
        encoding="utf-8",
    )


def test_chicang_three_day_minimum_and_current_redemption_reservation(tmp_path: Path) -> None:
    for day, qty in (("20260731", 270_077), ("20260803", 20_244), ("20260804", 44)):
        _write_position(tmp_path / day / "chicang3.csv", qty)
    snapshots = load_position_snapshots(tmp_path)
    event = RedemptionEvent("manual:reserve", "QMT3", date(2026, 8, 5), 40)
    result = position_inventory_snapshot(snapshots, (event,), TradingCalendar(), date(2026, 8, 5))
    assert result["QMT3"]["lookback_qtys"] == (270_077, 20_244, 44)
    assert result["QMT3"]["snapshot_eligible_qty"] == 44
    assert result["QMT3"]["reserved_qty"] == 40
    assert result["QMT3"]["eligible_qty"] == 4
    assert result["QMT3"]["total_qty"] == 4


def test_missing_position_snapshot_blocks_redemption(tmp_path: Path) -> None:
    _write_position(tmp_path / "20260804" / "chicang1.csv", 270_000)
    snapshots = load_position_snapshots(tmp_path)
    result = position_inventory_snapshot(snapshots, (), TradingCalendar(), date(2026, 8, 5))
    assert result["QMT1"]["confidence"] == "blocked"
    assert result["QMT1"]["eligible_qty"] == 0
    assert "缺少持仓快照" in result["QMT1"]["warnings"][0]


def test_official_calendar_holiday_and_coverage_gate() -> None:
    calendar = TradingCalendar()
    assert not calendar.is_trading_day(date(2026, 10, 1))
    with pytest.raises(CalendarCoverageError):
        calendar.is_trading_day(date(2027, 1, 4))


def test_statement_keeps_redemption_day_separate_from_settlement_day(tmp_path: Path) -> None:
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "证券代码,业务名称,交易日期,交收日期,成交数量,账户,净赎回款\n"
        "164824,基金赎回,2026-07-06,2026-07-13,270000,QMT1,300000\n",
        encoding="utf-8",
    )
    imported = load_redemption_statement(statement)
    assert imported.events[0].redeem_day == date(2026, 7, 6)
    assert imported.events[0].statement_day == date(2026, 7, 13)


def test_statement_without_account_is_blocked_instead_of_defaulting_qmt1(tmp_path: Path) -> None:
    statement = tmp_path / "statement.csv"
    statement.write_text(
        "证券代码,业务名称,交易日期,交收日期,成交数量,净赎回款\n"
        "164824,基金赎回,2026-07-06,2026-07-13,270000,300000\n",
        encoding="utf-8",
    )
    imported = load_redemption_statement(statement)
    assert imported.events == ()
    assert "QMT账户" in imported.issues[0].message


def test_t6_statement_status_advances_to_available() -> None:
    records = (_trade("QMT1", date(2026, 6, 1), "BUY", 270_000, "1"),)
    event = RedemptionEvent(
        "statement:available",
        "QMT1",
        date(2026, 6, 8),
        270_000,
        source="statement",
        net_amount=Decimal("270000"),
        statement_day=date(2026, 6, 15),
    )
    result = calculate(records, (event,), IndiaConfig(), as_of_day=date(2026, 6, 16))
    settlement = result.baskets[0].settlement
    assert settlement.actual_available_day == date(2026, 6, 16)
    assert settlement.status == "available"


def test_untagged_ib_fills_are_excluded_from_basket_profit() -> None:
    records = (_trade("QMT1", date(2026, 6, 1), "BUY", 270_000, "1"),)
    event = RedemptionEvent("manual:no-map", "QMT1", date(2026, 6, 8), 270_000, net_amount=Decimal("270000"))
    fills = (
        IbFill("other-open", "NIFTYM26", "期货", datetime(2026, 6, 8, 10), -1, Decimal("100")),
        IbFill("other-close", "NIFTYM26", "期货", datetime(2026, 6, 8, 11), 1, Decimal("90")),
    )
    result = calculate(records, (event,), IndiaConfig(), fx_rate=Decimal("6.8"), ib_fills=fills)
    assert result.baskets[0].hedge.nifty_pnl_usd == 0
    assert any("已隔离 2 笔" in warning for warning in result.warnings)


def test_ib_flex_header_parser_does_not_treat_open_close_code_as_order_ref(tmp_path: Path) -> None:
    flex = tmp_path / "flex.csv"
    flex.write_text(
        "交易,Header,DataDiscriminator,资产分类,货币,代码,日期/时间,数量,交易价格,收盘价格,收益,佣金/税,基础,已实现的损益,按市值计算的损益,代码\n"
        '交易,Data,Order,期货,USD,NIFTYM26,"2026-06-08, 10:00:00",-1,100,99,200,-2,-198,0,2,O\n',
        encoding="utf-8",
    )
    fills = load_ib_india_fills(flex)
    assert len(fills) == 1
    assert fills[0].order_ref == ""
