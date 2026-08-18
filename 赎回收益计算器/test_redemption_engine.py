from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import pandas as pd

import redemption_engine as engine


SAMPLE_ROOT = Path("/Users/ellis/Desktop/ETF交割/6.22")


class RedemptionEngineTest(unittest.TestCase):
    def calculate(self, overrides=None):
        return engine.calculate(
            {"QMT1": SAMPLE_ROOT / "qmt1.xlsx", "QMT2": None},
            SAMPLE_ROOT / "U15286908_20260601_20260629.csv",
            Decimal("6.79635"),
            overrides or {},
        )

    def test_first_basket_matches_audited_result(self) -> None:
        result = self.calculate()
        self.assertEqual(len(result.baskets), 4)
        self.assertGreater(len(result.qmt_records), 0)
        self.assertGreater(result.qmt_records[0].price, Decimal("0"))
        basket = result.baskets[0]
        self.assertEqual(basket.contract_no, 3800037544)
        self.assertEqual(engine.money(basket.domestic_cost), Decimal("1021303.57"))
        self.assertEqual(engine.money(basket.refund_amount), Decimal("1048660.95"))
        self.assertEqual(engine.money(basket.cash_difference), Decimal("-1114.32"))
        self.assertEqual(engine.money(basket.domestic_pnl), Decimal("26243.06"))
        self.assertEqual(basket.ib_trade_pnl_usd.quantize(engine.Q6), Decimal("-2638.086603"))
        self.assertEqual(engine.money(basket.ib_borrow_fee_usd), Decimal("0.00"))
        self.assertEqual(engine.money(basket.total_pnl_cny), Decimal("8313.70"))
        self.assertEqual(basket.status, "已结算")

    def test_cash_difference_stays_with_its_own_contract(self) -> None:
        result = self.calculate()
        first = result.baskets[0]
        fourth = result.baskets[3]
        self.assertEqual(engine.money(first.cash_difference), Decimal("-1114.32"))
        self.assertEqual(fourth.contract_no, 3800000002)
        self.assertEqual(engine.money(fourth.cash_difference), Decimal("1401.74"))

    def test_venue_sale_is_removed_before_redemption(self) -> None:
        result = self.calculate()
        first_close = result.venue_closes[0]
        self.assertEqual(first_close.contract_no, 3800024849)
        self.assertEqual(first_close.qty, 101000)
        self.assertEqual(engine.money(first_close.cost), Decimal("103732.19"))
        self.assertEqual(engine.money(first_close.pnl), Decimal("-414.36"))

    def test_manual_mapping_is_persistently_distinguishable(self) -> None:
        initial = self.calculate()
        basket = initial.baskets[0]
        override = {
            basket.id: {
                "open_trade_ids": [item.trade_id for item in basket.ib_open],
                "close_trade_ids": [item.trade_id for item in basket.ib_close],
            }
        }
        recalculated = self.calculate(override)
        mapped = recalculated.baskets[0]
        self.assertTrue(mapped.manual_ib_mapping)
        self.assertEqual(engine.money(mapped.total_pnl_cny), Decimal("8313.70"))

    def test_dynamic_hedge_target_changes_ib_allocation(self) -> None:
        initial = self.calculate()
        basket_id = initial.baskets[0].id
        recalculated = engine.calculate(
            {"QMT1": SAMPLE_ROOT / "qmt1.xlsx", "QMT2": None},
            SAMPLE_ROOT / "U15286908_20260601_20260629.csv",
            Decimal("6.79635"),
            hedge_targets={basket_id: 996},
        )
        self.assertEqual(initial.baskets[0].hedge_target, engine.DEFAULT_HEDGE_SHARES)
        self.assertEqual(recalculated.baskets[0].hedge_target, 996)
        self.assertEqual(sum(item.qty for item in recalculated.baskets[0].ib_open), 996)

    def test_hedge_target_rounds_up(self) -> None:
        self.assertEqual(
            engine.hedge_target_from_shares_per_cu(1_000_000, Decimal("995.3")),
            996,
        )

    def test_expected_receipt_dates_skip_manual_holidays(self) -> None:
        result = engine.calculate(
            {"QMT1": SAMPLE_ROOT / "qmt1.xlsx", "QMT2": None},
            SAMPLE_ROOT / "U15286908_20260601_20260629.csv",
            Decimal("6.79635"),
            market_holidays=(date(2026, 6, 29),),
        )
        first = result.baskets[0]
        self.assertEqual(first.expected_cash_difference_day, date(2026, 6, 25))
        self.assertEqual(first.expected_refund_day, date(2026, 7, 1))
        self.assertEqual(first.actual_cash_difference_day, date(2026, 6, 25))
        self.assertEqual(first.actual_refund_day, date(2026, 6, 30))

    @staticmethod
    def qmt_record(
        source: str,
        row: int,
        contract: int | str,
        action: str,
        qty: int,
        amount: str,
        trade_day: date = date(2026, 7, 1),
    ) -> engine.QmtRecord:
        return engine.QmtRecord(
            source=source,
            row_number=row,
            trade_day=trade_day,
            contract_no=contract,
            action=action,
            qty=qty,
            price=abs(Decimal(amount)) / Decimal(qty) if qty else Decimal("0"),
            amount=Decimal(amount),
            code=engine.TARGET_CODE,
            name="标普油气",
        )

    def test_qmt3_statement_fields_are_normalized_and_timestamped(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "qmt3.xlsx"
            pd.DataFrame(
                [
                    {
                        "成交日期": 20260709,
                        "成交时间": datetime(2026, 7, 9, 15, 0).time(),
                        "证券代码": 159518,
                        "证券名称": "标普油气ETF嘉实",
                        "操作": "买入",
                        "成交数量": 101000,
                        "成交均价": 1.088,
                        "发生金额": -109893.49,
                        "合同编号": "TJR30007",
                    },
                    {
                        "成交日期": 20260709,
                        "成交时间": datetime(2026, 7, 9, 15, 0).time(),
                        "证券代码": 159518,
                        "证券名称": "标普油气ETF嘉实",
                        "操作": "ETF 基金赎回",
                        "成交数量": 1_000_000,
                        "成交均价": 0,
                        "发生金额": 0,
                        "合同编号": "TJR30008",
                    },
                ]
            ).to_excel(path, index=False)

            records = engine.load_qmt_file(path, "QMT3")

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].action, "证券买入")
        self.assertEqual(records[0].contract_no, "TJR30007")
        self.assertEqual(records[0].trade_dt, datetime(2026, 7, 9, 15, 0))

    def test_qmt3_transfer_carries_full_two_system_cost_into_redemption(self) -> None:
        records = [
            self.qmt_record("QMT3", 1, "TJR30001", "证券买入", 1_000_000, "-1000000"),
            self.qmt_record("QMT3", 2, "TJR30002", "证券卖出", 1_000_000, "1010000"),
            self.qmt_record("QMT2", 1, 100, "证券买入", 1_000_000, "-1011000"),
            self.qmt_record("QMT2", 2, 200, "ETF 基金赎回", 1_000_000, "0"),
        ]

        baskets, venue_closes, transfers = engine.build_domestic_ledger(records)

        self.assertEqual(len(baskets), 1)
        self.assertEqual(len(venue_closes), 0)
        self.assertEqual(len(transfers), 1)
        transfer = transfers[0]
        self.assertEqual(transfer.kind, "QMT3 成本承接")
        self.assertEqual(transfer.sell_source, "QMT3")
        self.assertEqual(transfer.buy_source, "QMT2")
        self.assertEqual(engine.money(transfer.carried_cost or Decimal("0")), Decimal("1001000.00"))
        self.assertEqual(engine.money(baskets[0].domestic_cost), Decimal("1001000.00"))

    @staticmethod
    def ib_open_trade(
        trade_id: str,
        row: int,
        china_dt: datetime,
        qty: int,
        price: str,
    ) -> engine.IbTrade:
        ib_dt = engine.china_dt_to_ib_statement_dt(china_dt)
        return engine.IbTrade(
            id=f"{trade_id}:direct_open:{row}",
            row_number=row,
            dt=ib_dt,
            qty=-qty,
            price=Decimal(price),
            gross=Decimal(qty) * Decimal(price),
            commission=Decimal("1"),
            marker="AUTO_DIRECT_OPEN",
        )

    def test_qmt3_same_minute_open_link_uses_contract_then_ib_row_order(self) -> None:
        moment = datetime(2026, 7, 1, 10, 0, 45)
        later_contract = engine.replace(
            self.qmt_record("QMT3", 1, "TJR3000B", "证券买入", 101_000, "-101000"),
            trade_dt=moment,
        )
        earlier_contract = engine.replace(
            self.qmt_record("QMT3", 2, "TJR3000A", "证券买入", 101_000, "-101000"),
            trade_dt=moment.replace(second=5),
        )
        trades = [
            self.ib_open_trade("first", 10, moment.replace(second=10), 100, "10"),
            self.ib_open_trade("second", 11, moment.replace(second=20), 100, "20"),
        ]

        hedges, reserved, warnings = engine.build_qmt3_open_hedges(
            [later_contract, earlier_contract],
            trades,
        )

        self.assertEqual(hedges[("QMT3", 2)].slices[0].price, Decimal("10"))
        self.assertEqual(hedges[("QMT3", 1)].slices[0].price, Decimal("20"))
        self.assertEqual(reserved, {"first": 100, "second": 100})
        self.assertEqual(warnings, ())

    def test_qmt3_open_is_carried_proportionally_and_not_reused(self) -> None:
        moment = datetime(2026, 7, 1, 10, 0)
        records = [
            engine.replace(
                self.qmt_record("QMT3", 1, "TJR30001", "证券买入", 101_000, "-101000"),
                trade_dt=moment,
            ),
            self.qmt_record("QMT3", 2, "TJR30002", "证券卖出", 101_000, "101000"),
            self.qmt_record("QMT2", 1, 100, "证券买入", 101_000, "-101000"),
            self.qmt_record("QMT2", 2, 200, "ETF 基金赎回", 70_700, "0"),
        ]
        trades = [self.ib_open_trade("qmt3-open", 10, moment, 100, "10")]
        hedges, reserved, _warnings = engine.build_qmt3_open_hedges(records, trades)
        baskets, venue_closes, transfers = engine.build_domestic_ledger(records, qmt3_open_hedges=hedges)

        self.assertEqual(transfers[0].qmt3_hedge_target, 100)
        self.assertEqual(sum(item.qty for item in transfers[0].qmt3_hedge_open), 100)
        self.assertEqual(baskets[0].qmt3_hedge_target, 70)
        self.assertEqual(sum(item.qty for item in baskets[0].qmt3_hedge_open), 70)

        remaining_sell, _remaining_buy = engine.allocate_ib(
            baskets,
            venue_closes,
            trades,
            {},
            qmt3_reserved_sell_qty=reserved,
        )
        self.assertEqual(sum(item.qty for item in baskets[0].ib_open), 70)
        self.assertEqual(baskets[0].ib_open[0].role, "qmt3_carried_open")
        self.assertEqual(sum(remaining_sell.values()), 0)

    def test_qmt3_open_link_ignores_ib_sell_outside_china_session(self) -> None:
        moment = datetime(2026, 7, 1, 10, 0)
        record = engine.replace(
            self.qmt_record("QMT3", 1, "TJR30001", "证券买入", 202_000, "-202000"),
            trade_dt=moment,
        )
        trades = [
            self.ib_open_trade("session", 10, moment, 100, "10"),
            self.ib_open_trade("late", 11, datetime(2026, 7, 1, 20, 0), 100, "20"),
        ]

        hedges, reserved, warnings = engine.build_qmt3_open_hedges([record], trades)

        self.assertEqual(hedges[("QMT3", 1)].target_qty, 200)
        self.assertEqual(sum(item.qty for item in hedges[("QMT3", 1)].slices), 100)
        self.assertEqual(reserved, {"session": 100})
        self.assertIn("缺口 100 股", warnings[0])

    def test_qmt3_cannot_create_redemption_basket_and_unmatched_sale_stays_venue_close(self) -> None:
        records = [
            self.qmt_record("QMT3", 1, "TJR30001", "证券买入", 1_000_000, "-1000000"),
            self.qmt_record("QMT3", 2, "TJR30002", "证券卖出", 1_000_000, "1009000"),
            self.qmt_record("QMT3", 3, "TJR30003", "ETF 基金赎回", 1_000_000, "0"),
            self.qmt_record("QMT1", 1, 100, "证券买入", 1_000_000, "-1005000"),
            self.qmt_record("QMT1", 2, 200, "ETF 基金赎回", 1_000_000, "0"),
        ]

        baskets, venue_closes, transfers = engine.build_domestic_ledger(records)

        self.assertEqual([(item.source, item.contract_no) for item in baskets], [("QMT1", 200)])
        self.assertEqual(len(transfers), 0)
        self.assertEqual([(item.source, item.qty) for item in venue_closes], [("QMT3", 1_000_000)])

    def test_reused_contract_number_cash_flows_use_expected_window(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 1_000_000, "-1000000", date(2026, 6, 29)),
            self.qmt_record("QMT1", 3, 3800000002, "ETF 基金赎回", 1_000_000, "0", date(2026, 6, 29)),
            self.qmt_record("QMT1", 4, 200, "证券买入", 1_000_000, "-1000000", date(2026, 6, 30)),
            self.qmt_record("QMT1", 5, 3800000002, "ETF 基金赎回", 1_000_000, "0", date(2026, 6, 30)),
            self.qmt_record("QMT1", 6, 3800000002, "ETF 现金差额", 0, "1638.17", date(2026, 7, 2)),
            self.qmt_record("QMT1", 7, 3800000002, "ETF 申购退款", 0, "1049930", date(2026, 7, 8)),
        ]
        baskets, _, _ = engine.build_domestic_ledger(records)
        engine.attach_cash_flows(baskets, records, frozenset({date(2026, 7, 3)}))
        self.assertEqual(len(baskets), 2)
        self.assertEqual(baskets[0].redeem_day, date(2026, 6, 29))
        self.assertEqual(baskets[1].redeem_day, date(2026, 6, 30))
        self.assertEqual(engine.money(baskets[0].cash_difference), Decimal("1638.17"))
        self.assertEqual(engine.money(baskets[0].refund_amount), Decimal("1049930.00"))
        self.assertEqual(engine.money(baskets[1].cash_difference), Decimal("0.00"))
        self.assertEqual(engine.money(baskets[1].refund_amount), Decimal("0.00"))

    def test_cash_difference_uses_domestic_calendar_not_refund_holiday(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 1_000_000, "-1000000", date(2026, 6, 29)),
            self.qmt_record("QMT1", 3, 3800000002, "ETF 基金赎回", 1_000_000, "0", date(2026, 6, 29)),
            self.qmt_record("QMT1", 4, 200, "证券买入", 1_000_000, "-1000000", date(2026, 6, 30)),
            self.qmt_record("QMT1", 5, 3800000002, "ETF 基金赎回", 1_000_000, "0", date(2026, 6, 30)),
            self.qmt_record("QMT1", 6, 3800000002, "ETF 现金差额", 0, "1638.17", date(2026, 7, 2)),
            self.qmt_record("QMT1", 7, 3800000002, "ETF 现金差额", 0, "1182.59", date(2026, 7, 3)),
        ]
        baskets, _, _ = engine.build_domestic_ledger(records)
        engine.attach_cash_flows(baskets, records, refund_holidays=frozenset({date(2026, 7, 3)}))
        self.assertEqual(engine.money(baskets[0].cash_difference), Decimal("1638.17"))
        self.assertEqual(engine.money(baskets[1].cash_difference), Decimal("1182.59"))

    def test_manual_refund_is_used_until_qmt_refund_arrives(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 1_000_000, "-1000000", date(2026, 6, 29)),
            self.qmt_record("QMT1", 3, 300, "ETF 基金赎回", 1_000_000, "0", date(2026, 6, 29)),
            self.qmt_record("QMT1", 4, 300, "ETF 现金差额", 0, "1200", date(2026, 7, 2)),
        ]
        baskets, _, _ = engine.build_domestic_ledger(records)
        engine.attach_cash_flows(baskets, records)
        basket = baskets[0]
        engine.apply_manual_refund_overrides(
            baskets,
            {basket.id: {"manual_refund_amount": "1049930.12"}},
        )
        engine.finalize_baskets(baskets, Decimal("6.8"), date(2026, 7, 8), frozenset())

        self.assertEqual(engine.money(basket.refund_amount), Decimal("1049930.12"))
        self.assertEqual(basket.manual_refund_amount, Decimal("1049930.12"))
        self.assertTrue(basket.manual_refund_applied)
        self.assertIsNone(basket.actual_refund_day)
        self.assertEqual(basket.status, "已结算")
        self.assertIn("人工输入", "；".join(basket.warnings))

    def test_qmt_refund_takes_precedence_over_manual_refund(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 1_000_000, "-1000000", date(2026, 6, 29)),
            self.qmt_record("QMT1", 3, 300, "ETF 基金赎回", 1_000_000, "0", date(2026, 6, 29)),
            self.qmt_record("QMT1", 4, 300, "ETF 现金差额", 0, "1200", date(2026, 7, 2)),
            self.qmt_record("QMT1", 5, 300, "ETF 申购退款", 0, "2000000", date(2026, 7, 8)),
        ]
        baskets, _, _ = engine.build_domestic_ledger(records)
        engine.attach_cash_flows(baskets, records)
        basket = baskets[0]
        engine.apply_manual_refund_overrides(
            baskets,
            {basket.id: {"manual_refund_amount": "1049930.12"}},
        )
        engine.finalize_baskets(baskets, Decimal("6.8"), date(2026, 7, 8), frozenset())

        self.assertEqual(engine.money(basket.refund_amount), Decimal("2000000.00"))
        self.assertEqual(basket.manual_refund_amount, Decimal("1049930.12"))
        self.assertFalse(basket.manual_refund_applied)
        self.assertEqual(basket.actual_refund_day, date(2026, 7, 8))
        self.assertEqual(basket.status, "已结算")
        self.assertIn("交割单实际退款", "；".join(basket.warnings))

    def test_cross_account_rebalance_uses_separate_path(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 1_000_000, "-1000000"),
            self.qmt_record("QMT1", 3, 200, "证券卖出", 1_000_000, "1010000"),
            self.qmt_record("QMT2", 2, 205, "证券买入", 1_000_000, "-1005000"),
            self.qmt_record("QMT2", 3, 300, "ETF 基金赎回", 1_000_000, "0"),
        ]
        baskets, venue_closes, transfers = engine.build_domestic_ledger(records, transfer_contract_gap=10)
        self.assertEqual(len(transfers), 1)
        self.assertEqual(len(venue_closes), 0)
        self.assertEqual(transfers[0].sell_source, "QMT1")
        self.assertEqual(transfers[0].buy_source, "QMT2")
        self.assertEqual(sum(item.qty for item in transfers[0].matches), 1_000_000)
        self.assertEqual(engine.money(transfers[0].realized_pnl), Decimal("10000.00"))
        self.assertEqual(engine.money(baskets[0].domestic_cost), Decimal("1005000.00"))

    def test_distant_cross_account_trades_remain_ordinary(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 1_000_000, "-1000000"),
            self.qmt_record("QMT1", 3, 200, "证券卖出", 1_000_000, "1010000"),
            self.qmt_record("QMT2", 2, 2005, "证券买入", 1_000_000, "-1005000"),
            self.qmt_record("QMT2", 3, 3000, "ETF 基金赎回", 1_000_000, "0"),
        ]
        baskets, venue_closes, transfers = engine.build_domestic_ledger(records, transfer_contract_gap=10)
        self.assertEqual(len(transfers), 0)
        self.assertEqual(len(venue_closes), 1)
        self.assertEqual(engine.money(baskets[0].domestic_cost), Decimal("1005000.00"))

    def test_buy_first_cross_account_rebalance_is_identified(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 1_000_000, "-1000000"),
            self.qmt_record("QMT2", 2, 190, "证券买入", 1_000_000, "-1005000"),
            self.qmt_record("QMT1", 3, 200, "证券卖出", 1_000_000, "1010000"),
            self.qmt_record("QMT2", 3, 300, "ETF 基金赎回", 1_000_000, "0"),
        ]
        baskets, venue_closes, transfers = engine.build_domestic_ledger(records, transfer_contract_gap=10)
        self.assertEqual(len(transfers), 1)
        self.assertEqual(len(venue_closes), 0)
        self.assertEqual(transfers[0].buy_contract_no, 190)
        self.assertEqual(transfers[0].sell_contract_no, 200)
        self.assertEqual(transfers[0].contract_gap, 10)
        self.assertEqual(engine.money(baskets[0].domestic_cost), Decimal("1005000.00"))

    def test_same_day_cross_account_aggregate_transfer_is_identified(self) -> None:
        records = [
            self.qmt_record("QMT1", 2, 100, "证券买入", 505_000, "-505000", date(2026, 7, 1)),
            self.qmt_record("QMT2", 2, 1000, "证券买入", 202_000, "-202000", date(2026, 7, 1)),
            self.qmt_record("QMT2", 3, 1010, "证券买入", 202_000, "-202000", date(2026, 7, 1)),
            self.qmt_record("QMT2", 4, 1020, "证券买入", 101_000, "-101000", date(2026, 7, 1)),
            self.qmt_record("QMT1", 3, 5000, "证券卖出", 101_000, "101500", date(2026, 7, 1)),
            self.qmt_record("QMT1", 4, 5010, "证券卖出", 202_000, "203000", date(2026, 7, 1)),
            self.qmt_record("QMT1", 5, 5020, "证券卖出", 202_000, "203000", date(2026, 7, 1)),
        ]
        _, venue_closes, transfers = engine.build_domestic_ledger(records, transfer_contract_gap=10)
        self.assertEqual(len(venue_closes), 0)
        self.assertEqual(len(transfers), 3)
        self.assertEqual(sum(item.qty for item in transfers), 505_000)
        self.assertGreater(max(item.contract_gap for item in transfers), 10)

    @staticmethod
    def ib_trade(row: int, when: str, qty: int, price: str, marker: str = "O") -> engine.IbTrade:
        gross = abs(Decimal(qty) * Decimal(price))
        return engine.IbTrade(
            id=f"xop-{row}",
            row_number=row,
            dt=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
            qty=qty,
            price=Decimal(price),
            gross=gross,
            commission=Decimal("1"),
            marker=marker,
        )

    @staticmethod
    def stock_trade(row: int, when: str, symbol: str) -> engine.IbStockTrade:
        return engine.IbStockTrade(
            id=f"{symbol}-{row}",
            row_number=row,
            dt=datetime.strptime(when, "%Y-%m-%d %H:%M:%S"),
            symbol=symbol,
            qty=-100,
            price=Decimal("10"),
            gross=Decimal("1000"),
            commission=Decimal("1"),
            marker="O",
        )

    def test_component_short_window_creates_xop_base_and_synthetic_open(self) -> None:
        xop_trades = [
            self.ib_trade(1, "2026-07-01 09:30:00", -100, "100"),
            self.ib_trade(2, "2026-07-01 10:02:00", 150, "101", "C;O"),
            self.ib_trade(3, "2026-07-01 21:30:00", -50, "102", "C"),
        ]
        stock_trades = [
            self.stock_trade(index, "2026-07-01 10:00:00", f"C{index:02d}")
            for index in range(20)
        ]
        derived = engine.build_ib_hedge_trades(xop_trades, stock_trades)
        self.assertEqual([(item.qty, engine._trade_role(item)) for item in derived], [
            (-100, "direct_open"),
            (100, "direct_close"),
            (-50, "synthetic_open"),
        ])

    def test_venue_close_ib_buy_is_removed_from_basket_close_pool(self) -> None:
        records = [
            self.qmt_record("QMT1", 1, 100, "证券买入", 1_000_000, "-1000000", date(2026, 6, 22)),
            self.qmt_record("QMT1", 2, 200, "证券卖出", 101_000, "103000", date(2026, 6, 23)),
            self.qmt_record("QMT1", 3, 300, "ETF 基金赎回", 899_000, "0", date(2026, 6, 23)),
        ]
        baskets, venue_closes, _ = engine.build_domestic_ledger(records)
        trades = [
            self.ib_trade(1, "2026-06-22 20:00:00", -100, "100"),
            self.ib_trade(2, "2026-06-22 21:28:50", 100, "101"),
            self.ib_trade(3, "2026-06-23 10:00:00", -891, "102"),
            self.ib_trade(4, "2026-06-23 15:41:00", 891, "103"),
        ]
        remaining_sell, remaining_buy = engine.allocate_ib(baskets, venue_closes, trades, {}, Decimal("7"))
        self.assertEqual(sum(item.qty for item in venue_closes[0].ib_close), 100)
        self.assertEqual(sum(item.qty for item in baskets[0].ib_close), 891)
        self.assertEqual(baskets[0].ib_close[0].dt, datetime(2026, 6, 23, 15, 41))
        self.assertEqual(sum(remaining_buy.values()), 0)
        self.assertEqual(sum(remaining_sell.values()), 0)

    def test_china_session_starts_at_0900_and_includes_0928_ib_trade(self) -> None:
        trade_day = date(2026, 7, 13)
        before_open = self.ib_trade(1, "2026-07-12 20:59:59", 1, "100")
        at_open = self.ib_trade(2, "2026-07-12 21:00:00", 1, "100")
        actual_preopen_close = self.ib_trade(3, "2026-07-12 21:28:00", 1, "100")

        self.assertFalse(engine._is_china_session_trade(before_open, trade_day))
        self.assertTrue(engine._is_china_session_trade(at_open, trade_day))
        self.assertTrue(engine._is_china_session_trade(actual_preopen_close, trade_day))

    def test_ib_self_close_fifo_splits_commission_and_calculates_pnl(self) -> None:
        trades = [
            self.ib_trade(1, "2026-07-09 09:30:00", 100, "10"),
            engine.replace(
                self.ib_trade(2, "2026-07-09 10:00:00", -40, "12"),
                commission=Decimal("0.4"),
            ),
            engine.replace(
                self.ib_trade(3, "2026-07-09 10:30:00", -60, "11"),
                commission=Decimal("0.6"),
            ),
        ]

        pairs, unmatched = engine.match_ib_self_closes(
            trades,
            {"xop-2": 40, "xop-3": 60},
            {"xop-1": 100},
            Decimal("7"),
        )

        self.assertEqual([item.qty for item in pairs], [40, 60])
        self.assertEqual({item.direction for item in pairs}, {"先买后卖"})
        self.assertEqual(sum((item.trade_pnl_usd for item in pairs), Decimal("0")), Decimal("138.0"))
        self.assertEqual(sum((item.pnl_cny for item in pairs), Decimal("0")), Decimal("966.0"))
        self.assertEqual(unmatched, ())

    def test_ib_self_close_reconciles_pre_pair_residual_to_unmatched(self) -> None:
        result = self.calculate()

        self.assertEqual(
            result.residual_ib_sell_qty,
            result.ib_self_close_qty + result.unallocated_ib_sell_qty,
        )
        self.assertEqual(
            result.residual_ib_buy_qty,
            result.ib_self_close_qty + result.unallocated_ib_buy_qty,
        )

    def test_qmt_time_hints_match_full_contract_suffix(self) -> None:
        records = [
            self.qmt_record("QMT2", 1, 3800029805, "证券卖出", 101_000, "102313", date(2026, 6, 25)),
        ]
        hints = {
            ("QMT2", date(2026, 6, 25), "29805", "卖出", 101_000): datetime(2026, 6, 25, 11, 16, 2),
        }
        enriched = engine._enrich_qmt_record_time(records[0], hints)
        self.assertEqual(enriched.trade_dt, datetime(2026, 6, 25, 11, 16, 2))

    def test_manual_virtual_close_forces_domestic_rollover(self) -> None:
        records = [
            self.qmt_record("QMT1", 1, 100, "证券买入", 1_000_000, "-1000000", date(2026, 7, 3)),
            self.qmt_record("QMT1", 2, 200, "ETF 基金赎回", 1_000_000, "0", date(2026, 7, 6)),
            self.qmt_record("QMT1", 3, 300, "证券买入", 1_000_000, "-1010000", date(2026, 7, 7)),
        ]
        baskets, venue_closes, transfers = engine.build_domestic_ledger(records)
        basket = baskets[0]
        basket.hedge_target = engine.DEFAULT_HEDGE_SHARES
        trades = [
            self.ib_trade(1, "2026-07-06 14:00:00", -990, "100"),
            self.ib_trade(2, "2026-07-06 15:40:00", 990, "101"),
        ]
        engine.allocate_ib(baskets, venue_closes, trades, {}, Decimal("7"))

        automatic = engine.build_domestic_rollover_trades(
            baskets, venue_closes, transfers, records, Decimal("7"), {}
        )
        forced = engine.build_domestic_rollover_trades(
            baskets,
            venue_closes,
            transfers,
            records,
            Decimal("7"),
            {basket.id: {"manual_virtual_close": True}},
        )

        self.assertEqual(automatic, [])
        self.assertEqual([(item.qty, engine._trade_role(item)) for item in forced], [
            (990, "domestic_rollover_close"),
            (-990, "domestic_rollover_open"),
        ])
        self.assertTrue(basket.manual_virtual_close)

        combined = sorted([*trades, *forced], key=lambda item: (item.dt, item.row_number, item.id))
        engine.allocate_ib(
            baskets,
            venue_closes,
            combined,
            {basket.id: {"manual_virtual_close": True}},
            Decimal("7"),
        )
        self.assertTrue(all(item.role == "domestic_rollover_close" for item in basket.ib_close))

    def test_rollover_open_follows_domestic_fifo_to_actual_consumer(self) -> None:
        records = [
            self.qmt_record("QMT1", 1, 100, "证券买入", 2_000_000, "-2000000", date(2026, 7, 6)),
            self.qmt_record("QMT1", 2, 200, "ETF 基金赎回", 1_000_000, "0", date(2026, 7, 6)),
            self.qmt_record("QMT1", 3, 300, "证券买入", 1_000_000, "-1010000", date(2026, 7, 7)),
            self.qmt_record("QMT1", 4, 400, "ETF 基金赎回", 1_000_000, "0", date(2026, 7, 7)),
            self.qmt_record("QMT1", 5, 500, "ETF 基金赎回", 1_000_000, "0", date(2026, 7, 8)),
        ]
        baskets, venue_closes, transfers = engine.build_domestic_ledger(records)
        closing_basket, intervening_basket, consuming_basket = baskets
        overrides = {closing_basket.id: {"manual_virtual_close": True}}

        rollover_trades, links = engine._build_domestic_rollover_plan(
            baskets,
            venue_closes,
            transfers,
            records,
            Decimal("7"),
            overrides,
        )
        engine.attach_domestic_rollover_opens(baskets, links)

        self.assertEqual(len(links), 1)
        self.assertEqual(intervening_basket.domestic_rollover_target, 0)
        self.assertEqual(consuming_basket.domestic_rollover_target, 990)
        self.assertEqual(
            {item.role for item in consuming_basket.domestic_rollover_open},
            {"domestic_rollover_open"},
        )

        reserved = {link.open_slice.trade_id: link.open_slice.qty for link in links}
        engine.allocate_ib(
            baskets,
            venue_closes,
            rollover_trades,
            overrides,
            Decimal("7"),
            domestic_rollover_reserved_sell_qty=reserved,
        )
        self.assertEqual(sum(item.qty for item in intervening_basket.ib_open), 0)
        self.assertEqual(sum(item.qty for item in consuming_basket.ib_open), 990)
        self.assertTrue(
            all(item.role == "domestic_rollover_open" for item in consuming_basket.ib_open)
        )


if __name__ == "__main__":
    unittest.main()
