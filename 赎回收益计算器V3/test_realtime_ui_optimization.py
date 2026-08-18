from __future__ import annotations

import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication

import realtime_premium
from redemption_ui import RealtimePremiumTab


TRADING_DAY = date(2026, 7, 6)


def write_cached_target_pcf(cache_root: Path, cash_component: Decimal, *, target: Path | None = None) -> Path:
    path = target or (cache_root / TRADING_DAY.isoformat() / "xml" / "159518.xml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<PCFFile>\n"
        "  <SecurityID>159518</SecurityID>\n"
        f"  <EstimateCashComponent>{cash_component}</EstimateCashComponent>\n"
        f"  <TradingDay>{TRADING_DAY:%Y%m%d}</TradingDay>\n"
        "</PCFFile>\n",
        encoding="utf-8",
    )
    return path


def quotes(xop_bid: Decimal = Decimal("150")) -> tuple[
    realtime_premium.XopQuote,
    realtime_premium.SinaQuote,
    realtime_premium.CfetsQuote,
]:
    now = datetime(2026, 7, 6, 10, 0)
    xop = realtime_premium.XopQuote(
        xop_bid,
        xop_bid + Decimal("0.1"),
        xop_bid + Decimal("0.05"),
        now,
        "Live",
    )
    domestic = realtime_premium.SinaQuote(
        "159518",
        "标普油气",
        Decimal("1.050"),
        Decimal("1.051"),
        100_000,
        80_000,
        Decimal("1.050"),
        now,
        now,
        bids=tuple(
            realtime_premium.QuoteLevel(Decimal("1.050") - Decimal(index) / 1000, 100_000)
            for index in range(5)
        ),
        asks=tuple(
            realtime_premium.QuoteLevel(Decimal("1.051") + Decimal(index) / 1000, 80_000)
            for index in range(5)
        ),
    )
    cfets = realtime_premium.CfetsQuote(
        Decimal("7"), TRADING_DAY, "10:00", "2026-07-06T10:00:00"
    )
    return xop, domestic, cfets


class RealtimeUiOptimizationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def make_tab(root: Path, *, shared: bool = False) -> RealtimePremiumTab:
        return RealtimePremiumTab(
            {
                "fx_rates_csv_path": str(root / "fx.csv"),
                "szse_pcf_cache_dir": str(root / "pcf"),
                "shared_folder_path": str(root / "shared") if shared else "",
            }
        )

    @staticmethod
    def close_tab(tab: RealtimePremiumTab) -> None:
        tab.shutdown()
        tab.close()

    def test_burst_quotes_are_coalesced_and_publish_latest_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_cached_target_pcf(root / "pcf", Decimal("1500"))
            tab = self.make_tab(root, shared=True)
            xop, domestic, cfets = quotes()

            with (
                patch.object(tab, "write_shared_snapshot", return_value=True) as writer,
                patch.object(
                    tab, "_flush_realtime_render", wraps=tab._flush_realtime_render
                ) as render,
            ):
                tab.update_domestic_quote(domestic)
                tab.update_cfets_quote(cfets)
                for index in range(20):
                    bid = Decimal("150") + Decimal(index) / Decimal("100")
                    tab.update_xop_quote(
                        replace(
                            xop,
                            bid=bid,
                            ask=bid + Decimal("0.1"),
                            last=bid + Decimal("0.05"),
                        )
                    )

                render.assert_not_called()
                writer.assert_not_called()
                tab._flush_realtime_render()
                tab._publish_latest_snapshot()
                self.assertEqual(render.call_count, 1)
                self.assertEqual(writer.call_count, 1)

                latest_bid = Decimal("150.19")
                self.assertEqual(tab.xop_quote.bid, latest_bid)
                expected = realtime_premium.calculate_premium_valuation(
                    latest_bid,
                    latest_bid + Decimal("0.1"),
                    cfets.rate,
                    domestic.bid,
                    domestic.ask,
                    estimate_cash_component_cny=Decimal("1500"),
                    pcf_trading_day=TRADING_DAY,
                )
                self.assertEqual(tab._latest_valuation, expected)
                self.assertEqual(tab.valuation_table.item(4, 1).text(), f"{expected.nav_bid:.6f}")

                tab._publish_latest_snapshot()
                self.assertEqual(writer.call_count, 1)
                tab._publish_latest_snapshot(force=True)
                self.assertEqual(writer.call_count, 2)
            self.close_tab(tab)

    def test_actual_timers_render_at_200ms_and_publish_at_500ms(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_cached_target_pcf(root / "pcf", Decimal("1500"))
            tab = self.make_tab(root, shared=True)
            xop, domestic, cfets = quotes()
            with patch.object(tab, "write_shared_snapshot", return_value=True) as writer:
                tab.update_xop_quote(xop)
                tab.update_domestic_quote(domestic)
                tab.update_cfets_quote(cfets)
                QTest.qWait(260)
                self.assertEqual(tab.valuation_table.rowCount(), 8)
                self.assertEqual(tab.valuation_table.item(0, 1).text(), "996 股")
                writer.assert_not_called()
                QTest.qWait(300)
                self.assertEqual(writer.call_count, 1)
            self.close_tab(tab)

    def test_pcf_is_parsed_once_per_file_signature_and_errors_are_cached(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pcf_root = root / "pcf"
            pcf_path = write_cached_target_pcf(pcf_root, Decimal("1500"))
            tab = self.make_tab(root)
            xop, domestic, cfets = quotes()

            with patch.object(
                tab.pcf_store,
                "ensure_target_detail",
                wraps=tab.pcf_store.ensure_target_detail,
            ) as parser:
                tab.update_xop_quote(xop)
                tab.update_domestic_quote(domestic)
                tab.update_cfets_quote(cfets)
                self.assertEqual(parser.call_count, 1)

                for index in range(20):
                    tab.update_xop_quote(
                        replace(xop, bid=xop.bid + Decimal(index) / Decimal("1000"))
                    )
                self.assertEqual(parser.call_count, 1)

                replacement = pcf_path.with_suffix(".replacement")
                write_cached_target_pcf(pcf_root, Decimal("1700"), target=replacement)
                os.replace(replacement, pcf_path)
                tab._heartbeat_refresh()
                self.assertEqual(parser.call_count, 2)
                self.assertEqual(tab.pcf_estimate_cash_component, Decimal("1700"))
                self.assertEqual(
                    tab._latest_valuation.estimate_cash_component_cny,
                    Decimal("1700"),
                )

                corrupt = pcf_path.with_suffix(".corrupt")
                corrupt.write_text("<PCFFile>", encoding="utf-8")
                os.replace(corrupt, pcf_path)
                tab._heartbeat_refresh()
                self.assertEqual(parser.call_count, 3)
                self.assertIsNone(tab._latest_valuation)
                self.assertTrue(tab.pcf_detail_error)

                tab.update_xop_quote(replace(xop, bid=Decimal("151")))
                self.assertEqual(parser.call_count, 3)
                self.assertIsNone(tab._latest_valuation)
            self.close_tab(tab)

    def test_identical_polls_only_refresh_timestamps_until_heartbeat(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            write_cached_target_pcf(root / "pcf", Decimal("1500"))
            tab = self.make_tab(root)
            xop, domestic, cfets = quotes()
            tab.update_xop_quote(xop)
            tab.update_domestic_quote(domestic)
            tab.update_cfets_quote(cfets)

            with patch.object(tab, "_queue_realtime_refresh") as queue_refresh:
                later = xop.received_at + timedelta(seconds=3)
                tab.update_xop_quote(replace(xop, received_at=later))
                tab.update_domestic_quote(replace(domestic, received_at=later))
                tab.update_cfets_quote(replace(cfets, fetched_at="later"))
                queue_refresh.assert_not_called()
                self.assertEqual(tab.xop_quote.received_at, later)
                self.assertEqual(tab.domestic_quote.received_at, later)
                self.assertEqual(tab.cfets_quote.fetched_at, "later")
            self.close_tab(tab)

    def test_missing_pcf_never_falls_back_to_zero(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = self.make_tab(root)
            xop, domestic, cfets = quotes()
            tab.update_xop_quote(xop)
            tab.update_domestic_quote(domestic)
            tab.update_cfets_quote(cfets)

            self.assertIsNone(tab.pcf_estimate_cash_component)
            self.assertIsNone(tab._latest_valuation)
            self.assertIn("等待当日159518 PCF缓存", tab._latest_missing)
            self.close_tab(tab)


if __name__ == "__main__":
    unittest.main()
