from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, datetime
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import ib_data_source
import redemption_engine


REPORT_XML = b"""<?xml version="1.0"?>
<FlexQueryResponse>
  <FlexStatements count="1">
    <FlexStatement fromDate="20260701" toDate="20260702">
      <Trades>
        <Order assetCategory="STK" currency="USD" symbol="XOP"
          dateTime="20260701;150311" tradeDate="20260701" quantity="-100"
          tradePrice="140.25" proceeds="14025" ibCommission="-1.25"
          notes="IA;O" ibOrderID="1001" />
        <Order assetCategory="STK" currency="USD" symbol="APA"
          dateTime="20260702;100832" tradeDate="20260702" quantity="50"
          tradePrice="32.315" tradeMoney="-1615.75" ibCommission="-0.50"
          notes="O" ibOrderID="1002" />
      </Trades>
    </FlexStatement>
  </FlexStatements>
</FlexQueryResponse>
"""


class IbDataSourceTests(unittest.TestCase):
    def test_latest_available_date_uses_previous_weekday(self) -> None:
        ny = ZoneInfo("America/New_York")
        monday = datetime(2026, 7, 20, 9, tzinfo=ny)
        self.assertEqual(ib_data_source.latest_available_date(monday), date(2026, 7, 17))
        friday = datetime(2026, 7, 17, 9, tzinfo=ny)
        self.assertEqual(ib_data_source.latest_available_date(friday), date(2026, 7, 16))

    def test_long_range_is_split_into_flex_safe_chunks(self) -> None:
        chunks = list(
            ib_data_source.iter_date_chunks(
                date(2025, 1, 1),
                date(2026, 1, 1),
            )
        )
        self.assertEqual(chunks[0], (date(2025, 1, 1), date(2025, 12, 31)))
        self.assertEqual(chunks[1], (date(2026, 1, 1), date(2026, 1, 1)))

    def test_xml_is_converted_to_engine_compatible_csv(self) -> None:
        records = ib_data_source.extract_activity_records(REPORT_XML)
        with tempfile.TemporaryDirectory() as directory:
            path = ib_data_source.write_activity_csv(
                records,
                Path(directory) / "activity.csv",
            )
            xop_trades, fees = redemption_engine.load_ib_statement(path)
            stock_trades = redemption_engine.load_ib_stock_trades(path)

            self.assertEqual(len(xop_trades), 1)
            self.assertEqual(xop_trades[0].qty, -100)
            self.assertEqual(str(xop_trades[0].commission), "1.25")
            self.assertEqual(xop_trades[0].marker, "IA;O")
            self.assertEqual(fees, [])
            self.assertEqual({item.symbol for item in stock_trades}, {"XOP", "APA"})

    def test_refresh_deduplicates_chunks_and_writes_metadata(self) -> None:
        calls: list[tuple[date, date]] = []

        def downloader(_token: str, _query: str, start: date, end: date) -> bytes:
            calls.append((start, end))
            return REPORT_XML

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ib_data_source.ib_flex,
            "load_token",
            return_value=("secret", "test"),
        ):
            result = ib_data_source.refresh_flex_csv(
                date(2025, 1, 1),
                "1574404",
                directory,
                end_date=date(2026, 1, 1),
                downloader=downloader,
            )
            self.assertTrue(result.refreshed)
            self.assertEqual(result.row_count, 2)
            self.assertEqual(len(calls), 2)
            self.assertTrue(result.csv_path.is_file())
            self.assertEqual(ib_data_source.load_metadata(directory)["row_count"], 2)

    def test_configured_token_takes_priority_without_credential_store(self) -> None:
        received_tokens: list[str] = []

        def downloader(token: str, _query: str, _start: date, _end: date) -> bytes:
            received_tokens.append(token)
            return REPORT_XML

        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ib_data_source.ib_flex,
            "load_token",
        ) as load_token:
            result = ib_data_source.refresh_flex_csv(
                date(2026, 7, 1),
                "1574404",
                directory,
                end_date=date(2026, 7, 2),
                configured_token="saved-config-token",
                downloader=downloader,
            )
            self.assertTrue(result.refreshed)
            self.assertEqual(received_tokens, ["saved-config-token"])
            load_token.assert_not_called()

    def test_failed_refresh_falls_back_to_last_complete_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(
            ib_data_source.ib_flex,
            "load_token",
            return_value=("secret", "test"),
        ):
            first = ib_data_source.refresh_flex_csv(
                date(2026, 7, 1),
                "1574404",
                directory,
                end_date=date(2026, 7, 2),
                downloader=lambda *_args: REPORT_XML,
            )
            fallback = ib_data_source.refresh_flex_csv(
                date(2026, 7, 1),
                "1574404",
                directory,
                end_date=date(2026, 7, 3),
                downloader=lambda *_args: (_ for _ in ()).throw(RuntimeError("offline")),
            )
            self.assertEqual(fallback.csv_path, first.csv_path)
            self.assertFalse(fallback.refreshed)
            self.assertIn("使用最近一次本地缓存", fallback.warning)
            with fallback.csv_path.open(encoding="utf-8-sig", newline="") as handle:
                data_rows = [row for row in csv.reader(handle) if len(row) > 1 and row[1] == "Data"]
            self.assertEqual(len(data_rows), 2)


if __name__ == "__main__":
    unittest.main()
