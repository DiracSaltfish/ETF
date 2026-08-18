from __future__ import annotations

import asyncio
import tempfile
import threading
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import backfill_xop_from_tws
from market_data import CsvXopPriceProvider, XopDailyPrice, upsert_xop_prices


class CsvXopPriceProviderTest(unittest.TestCase):
    def test_reads_close_and_preserves_decimal_precision(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "xop.csv"
            path.write_text(
                "symbol,trade_day,close,vwap_1540_1550,vwap_1540_1600,vwap_1554_1557,last_1600,source\n"
                "XOP,2026-07-01,158.2301,158.10,158.15,158.17,158.20,manual\n",
                encoding="utf-8",
            )
            provider = CsvXopPriceProvider(path)
            self.assertEqual(provider.get_close(date(2026, 7, 1)), Decimal("158.2301"))
            self.assertEqual(provider.get_window_price(date(2026, 7, 1), "1540_1550"), Decimal("158.10"))
            self.assertEqual(provider.get_window_price(date(2026, 7, 1), "1554_1557"), Decimal("158.17"))

    def test_missing_vwap_falls_back_to_close(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "xop.csv"
            path.write_text(
                "symbol,trade_day,close,vwap_1540_1550,vwap_1540_1600,last_1600,source\n"
                "XOP,2026-07-01,158.23,,,,manual\n",
                encoding="utf-8",
            )
            provider = CsvXopPriceProvider(path)
            self.assertEqual(provider.get_window_price(date(2026, 7, 1), "1540_1550"), Decimal("158.23"))
            self.assertEqual(provider.get_window_price(date(2026, 7, 1), "1540_1600"), Decimal("158.23"))
            self.assertEqual(provider.get_window_price(date(2026, 7, 1), "1554_1557"), Decimal("158.23"))

    def test_upsert_replaces_same_trade_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "xop.csv"
            upsert_xop_prices(
                path,
                [XopDailyPrice("XOP", date(2026, 7, 1), Decimal("158.23"))],
            )
            upsert_xop_prices(
                path,
                [XopDailyPrice("XOP", date(2026, 7, 1), Decimal("158.25"), source="tws")],
            )
            prices = CsvXopPriceProvider(path).load_prices()
            self.assertEqual(len(prices), 1)
            self.assertEqual(prices[0].close, Decimal("158.25"))
            self.assertIn("vwap_1554_1557", path.read_text(encoding="utf-8").splitlines()[0])


class TWSBackfillThreadTest(unittest.TestCase):
    def test_worker_thread_receives_private_asyncio_event_loop(self) -> None:
        observed: dict[str, object] = {}

        def worker() -> None:
            try:
                asyncio.get_event_loop()
                observed["had_loop"] = True
            except RuntimeError:
                observed["had_loop"] = False
            loop, owns_loop = backfill_xop_from_tws._ensure_asyncio_event_loop()
            observed["owns_loop"] = owns_loop
            observed["current_loop"] = asyncio.get_event_loop() is loop
            if owns_loop:
                loop.close()
                asyncio.set_event_loop(None)

        thread = threading.Thread(target=worker, name="prediction-test-worker")
        thread.start()
        thread.join(timeout=5)

        self.assertFalse(thread.is_alive())
        self.assertFalse(observed["had_loop"])
        self.assertTrue(observed["owns_loop"])
        self.assertTrue(observed["current_loop"])


if __name__ == "__main__":
    unittest.main()
