#!/usr/bin/env python3

import tempfile
import unittest
from datetime import date
from pathlib import Path

from etf_pcf_service import (
    PcfService,
    classify_intraday_opportunity,
    classify_opportunity,
)


PCF_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<PCFFile>
  <SecurityID>159518</SecurityID>
  <Symbol>PCF Test ETF</Symbol>
  <TradingDay>20260811</TradingDay>
  <CreationRedemptionUnit>1000000.00</CreationRedemptionUnit>
  <Creation>Y</Creation>
  <Redemption>Y</Redemption>
  <NetCreationLimit>3000000.00</NetCreationLimit>
  <NetRedemptionLimit>2000000.00</NetRedemptionLimit>
  <Components>
    <Component><UnderlyingSecurityID>XOP</UnderlyingSecurityID><ComponentShare>996</ComponentShare></Component>
  </Components>
</PCFFile>
"""


class PcfServiceTest(unittest.TestCase):
    def test_fetch_date_cache_and_direction_classification(self) -> None:
        calls = []

        def fetch(url):
            calls.append(url)
            return PCF_XML

        with tempfile.TemporaryDirectory() as directory:
            service = PcfService(
                Path(directory), fetch_bytes=fetch, min_request_interval_seconds=0
            )
            detail = service.ensure_symbol("159518", date(2026, 8, 11))
            self.assertEqual(detail["trading_day"], "2026-08-11")
            self.assertEqual(detail["creation_redemption_unit"], 1_000_000)
            self.assertEqual(detail["component_count"], 1)
            self.assertEqual(len(calls), 1)

            cached_service = PcfService(
                Path(directory),
                fetch_bytes=lambda _url: self.fail("date cache should avoid network"),
                min_request_interval_seconds=0,
            )
            cached = cached_service.ensure_symbol("159518", date(2026, 8, 11))
            self.assertEqual(cached["status"], "ready")
            summary = cached_service.summary_for("159518")
            creation = classify_opportunity(
                1_000_000, summary, reference_day=date(2026, 8, 11)
            )
            redemption = classify_opportunity(
                -2_000_000, summary, reference_day=date(2026, 8, 11)
            )
            self.assertEqual(creation["label"], "申购机会")
            self.assertEqual(creation["full_baskets"], 1)
            self.assertAlmostEqual(creation["limit_utilization"], 1 / 3)
            self.assertEqual(redemption["label"], "赎回机会")
            self.assertEqual(redemption["full_baskets"], 2)
            self.assertTrue(redemption["limit_reached"])

    def test_partial_basket_is_not_actionable(self) -> None:
        pcf = {
            "status": "ready",
            "trading_day": "2026-08-11",
            "creation_redemption_unit": 1_000_000,
            "creation_allowed": True,
            "redemption_allowed": True,
        }
        result = classify_opportunity(
            500_000, pcf, reference_day=date(2026, 8, 11)
        )
        self.assertEqual(result["kind"], "partial")
        self.assertFalse(result["actionable"])

    def test_intraday_reverse_flow_releases_opposite_capacity(self) -> None:
        pcf = {
            "status": "ready",
            "trading_day": "2026-08-11",
            "creation_redemption_unit": 1_000_000,
            "creation_allowed": True,
            "redemption_allowed": True,
        }
        opening = {"etfbuyamount": 1_000_000, "etfsellamount": 0, "netamount": 1_000_000}
        baseline = classify_intraday_opportunity(
            None, opening, pcf, reference_day=date(2026, 8, 11)
        )
        self.assertEqual(baseline["kind"], "baseline")
        self.assertFalse(baseline["actionable"])

        after_redemption = {
            "etfbuyamount": 1_000_000,
            "etfsellamount": 1_000_000,
            "netamount": 0,
        }
        creation = classify_intraday_opportunity(
            opening, after_redemption, pcf, reference_day=date(2026, 8, 11)
        )
        self.assertEqual(creation["label"], "盘中申购机会")
        self.assertEqual(creation["released_capacity_shares"], 1_000_000)
        self.assertTrue(creation["actionable"])

        after_creation = {"etfbuyamount": 2_000_000, "etfsellamount": 0, "netamount": 2_000_000}
        redemption = classify_intraday_opportunity(
            opening, after_creation, pcf, reference_day=date(2026, 8, 11)
        )
        self.assertEqual(redemption["label"], "盘中赎回机会")
        self.assertTrue(redemption["actionable"])


if __name__ == "__main__":
    unittest.main()
