from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

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
    def qmt_record(source: str, row: int, contract: int, action: str, qty: int, amount: str) -> engine.QmtRecord:
        return engine.QmtRecord(
            source=source,
            row_number=row,
            trade_day=date(2026, 7, 1),
            contract_no=contract,
            action=action,
            qty=qty,
            price=abs(Decimal(amount)) / Decimal(qty) if qty else Decimal("0"),
            amount=Decimal(amount),
            code=engine.TARGET_CODE,
            name="标普油气",
        )

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


if __name__ == "__main__":
    unittest.main()
