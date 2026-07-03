from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date

import fx_rates


SAMPLE_DAY = date(2026, 7, 3)
SAMPLE_SAFE_HTML = """
<html><body>
<table id="InfoTable">
  <tr><th>日期</th><th>美元</th><th>欧元</th><th>日元</th><th>韩元</th></tr>
  <tr><td>2026-07-03</td><td>680.47</td><td>776.09</td><td>4.2094</td><td>22710.0</td></tr>
</table>
</body></html>
"""
SAMPLE_CFETS_JSON = {
    "data": {
        "flag": "0",
        "startDateTool": "03 Jul 2026",
        "endDateTool": "03 Jul 2026",
        "startTime": "09:30",
        "endTime": "23:30",
    },
    "records": [
        {
            "ccyPair": "USD/CNY",
            "dealDate": "2026-07-03",
            "rateOf09hour": "---",
            "rateOf10hour": "6.7806",
            "rateOf11hour": "6.7809",
            "rateOf14hour": "6.7797",
            "rateOf15hour": "6.7799",
            "rateOf16hour": "6.7810",
            "rateOf17hour": "6.7811",
            "rateOf18hour": "6.7831",
            "rateOf19hour": "6.7816",
            "rateOf20hour": "---",
        },
        {
            "ccyPair": "EUR/CNY",
            "dealDate": "2026-07-03",
            "rateOf10hour": "7.7549",
            "rateOf11hour": "7.7565",
            "rateOf14hour": "7.7620",
            "rateOf15hour": "7.7653",
            "rateOf16hour": "7.7673",
            "rateOf17hour": "/",
        },
    ],
}


class FxRateStoreTest(unittest.TestCase):
    def test_safe_rates_are_normalized_and_keep_raw_values(self) -> None:
        store = fx_rates.FxRateStore("/tmp/unused.csv", fetch_bytes=lambda url, data=None, headers=None: SAMPLE_SAFE_HTML.encode("utf-8"))
        rows = store.fetch_safe_records(SAMPLE_DAY)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["pair"], "USD/CNY")
        self.assertEqual(rows[0]["rate"], "6.8047")
        self.assertEqual(rows[0]["raw_rate"], "680.47")

    def test_cfets_hourly_rates_generate_close_row(self) -> None:
        payload = json.dumps(SAMPLE_CFETS_JSON, ensure_ascii=False).encode("utf-8")
        store = fx_rates.FxRateStore("/tmp/unused.csv", fetch_bytes=lambda url, data=None, headers=None: payload)
        rows = store.fetch_cfets_records(SAMPLE_DAY)
        usd_close = next(row for row in rows if row["pair"] == "USD/CNY" and row["quote_time"] == "CLOSE")
        self.assertEqual(usd_close["rate"], "6.7831")
        self.assertEqual(usd_close["derived_from"], "18:00")
        self.assertTrue(all(row["pair"] == "USD/CNY" for row in rows))
        self.assertNotIn("19:00", {row["quote_time"] for row in rows})

    def test_ensure_trade_date_writes_csv_and_builds_matrix(self) -> None:
        def fetcher(url, data=None, headers=None):
            if "safe.gov.cn" in url:
                return SAMPLE_SAFE_HTML.encode("utf-8")
            if "cm-u-bk-fx/RefRateHis" in url:
                return json.dumps(SAMPLE_CFETS_JSON, ensure_ascii=False).encode("utf-8")
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = fx_rates.FxRateStore(f"{temp_dir}/fx_rates.csv", fetch_bytes=fetcher)
            store.ensure_trade_date(SAMPLE_DAY)
            rows = store.load_day_records(SAMPLE_DAY)
            self.assertGreater(len(rows), 0)

            hours, matrix = store.build_day_matrix(SAMPLE_DAY)
            self.assertEqual(hours, ["10:00", "11:00", "14:00", "15:00", "16:00", "17:00", "18:00"])
            self.assertEqual(len(matrix), 1)
            usd = matrix[0]
            self.assertEqual(usd["safe_rate"], "6.8047")
            self.assertEqual(usd["16:00"], "6.781")
            self.assertEqual(usd["18:00"], "6.7831")
            self.assertEqual(usd["close_rate"], "6.7831")
            self.assertEqual(usd["close_time"], "18:00")


if __name__ == "__main__":
    unittest.main()
