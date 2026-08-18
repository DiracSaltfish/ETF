from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path

import backfill_pcf_two_months
import szse_pcf


class PcfTwoMonthBackfillTest(unittest.TestCase):
    def test_subtract_calendar_months_clamps_to_month_end(self) -> None:
        self.assertEqual(
            backfill_pcf_two_months.subtract_calendar_months(date(2026, 3, 31), 1),
            date(2026, 2, 28),
        )
        self.assertEqual(
            backfill_pcf_two_months.subtract_calendar_months(date(2026, 7, 10), 2),
            date(2026, 5, 10),
        )

    def test_task_build_skips_existing_xml_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_root = Path(temp_dir)
            trading_day = date(2026, 7, 3)
            xml_path = cache_root / trading_day.isoformat() / "xml" / "159518.xml"
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_text("<PCFFile />", encoding="utf-8")

            tasks = backfill_pcf_two_months.build_missing_tasks(
                cache_root,
                trading_day,
                trading_day,
                interval_seconds=5,
            )

            self.assertNotIn(
                (trading_day, "159518"),
                {(item.trading_day, item.fund_code) for item in tasks[szse_pcf.EXCHANGE_SZSE]},
            )
            self.assertIn(
                (trading_day, "159501"),
                {(item.trading_day, item.fund_code) for item in tasks[szse_pcf.EXCHANGE_SZSE]},
            )


if __name__ == "__main__":
    unittest.main()
