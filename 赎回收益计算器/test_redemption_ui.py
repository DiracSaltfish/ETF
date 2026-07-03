from __future__ import annotations

import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

from PyQt5.QtWidgets import QApplication

import redemption_engine as engine
from redemption_ui import BasketMappingTab, normalize_business_day, pcf_field_reference_day_text, shift_business_day


SAMPLE_ROOT = Path("/Users/ellis/Desktop/ETF交割/6.22")


class BasketMappingTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.result = engine.calculate(
            {"QMT1": SAMPLE_ROOT / "qmt1.xlsx", "QMT2": None},
            SAMPLE_ROOT / "U15286908_20260601_20260629.csv",
            Decimal("6.79635"),
        )

    def setUp(self) -> None:
        self.tab = BasketMappingTab()
        self.tab.update_data(self.result)

    def tearDown(self) -> None:
        self.tab.close()

    def test_all_baskets_have_domestic_and_ib_links(self) -> None:
        self.assertEqual(self.tab.basket_table.rowCount(), 7)
        for basket in self.result.baskets:
            self.assertTrue(self.tab.domestic_basket_rows[basket.id])
            self.assertTrue(self.tab.ib_basket_rows[basket.id])

    def test_single_day_filter_keeps_only_that_redemption(self) -> None:
        selected = date(2026, 6, 22)
        value = self.tab._qdate(selected)
        self.tab.start_date.setDate(value)
        self.tab.end_date.setDate(value)
        self.tab.populate()
        self.assertEqual(self.tab.basket_table.rowCount(), 1)
        self.assertEqual(list(self.tab.basket_rows), [self.result.baskets[0].id])


class BusinessDayHelperTest(unittest.TestCase):
    def test_normalize_business_day_moves_weekend_back_to_friday(self) -> None:
        self.assertEqual(normalize_business_day(date(2026, 7, 4)), date(2026, 7, 3))
        self.assertEqual(normalize_business_day(date(2026, 7, 5)), date(2026, 7, 3))

    def test_shift_business_day_skips_weekends(self) -> None:
        self.assertEqual(shift_business_day(date(2026, 7, 3), 1), date(2026, 7, 6))
        self.assertEqual(shift_business_day(date(2026, 7, 6), -1), date(2026, 7, 3))

    def test_pcf_field_reference_day_uses_pre_trading_day_for_nav_fields(self) -> None:
        metadata = {
            "TradingDay": "20260622",
            "PreTradingDay": "20260617",
        }
        self.assertEqual(pcf_field_reference_day_text(metadata, "CashComponent", date(2026, 6, 22)), "2026-06-17")
        self.assertEqual(pcf_field_reference_day_text(metadata, "NAVperCU", date(2026, 6, 22)), "2026-06-17")
        self.assertEqual(pcf_field_reference_day_text(metadata, "NAV", date(2026, 6, 22)), "2026-06-17")
        self.assertEqual(pcf_field_reference_day_text(metadata, "RedemptionLimit", date(2026, 6, 22)), "2026-06-22")


if __name__ == "__main__":
    unittest.main()
