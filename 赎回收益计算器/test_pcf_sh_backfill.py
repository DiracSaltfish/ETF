from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

import szse_pcf


BACKFILL_ROOT = Path(__file__).resolve().parent.parent / "PCF上海回补"
sys.path.insert(0, str(BACKFILL_ROOT))
import backfill_sh_pcf  # noqa: E402


class ShanghaiPcfBackfillTest(unittest.TestCase):
    def setUp(self) -> None:
        backfill_sh_pcf.FULLGOAL_NAV_HISTORY.clear()
        backfill_sh_pcf.PUBLIC_NAV_HISTORY.clear()

    def test_partial_sse_xml_is_readable_by_existing_store(self) -> None:
        trading_day = date(2026, 7, 10)
        spec = backfill_sh_pcf.SPEC_BY_CODE["513350"]
        payload = backfill_sh_pcf.BackfillPayload(
            metadata={
                "FundInstrumentID": "513350",
                "FundName": "标普油气ETF富国",
                "TradingDay": "2026-07-10",
                "NAVSourceDay": "2026-07-09",
                "NAV": "1.1347",
            },
            components=(),
            source_url="https://example.test/nav",
            source_kind="基金公司历史净值映射（部分字段）",
            raw_files=(),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target = backfill_sh_pcf.cache_path(root, trading_day, spec.code)
            backfill_sh_pcf.write_payload_xml(target, spec, trading_day, payload)

            store = szse_pcf.SzsePcfStore(root)
            self.assertTrue(store.is_fund_detail_cached(trading_day, spec.code, szse_pcf.EXCHANGE_SSE))
            detail = store.ensure_sse_fund_detail(trading_day, spec.code)

            self.assertEqual(detail.metadata["TradingDay"], "2026-07-10")
            self.assertEqual(detail.metadata["NAV"], "1.1347")
            self.assertEqual(detail.metadata["NAVSourceDay"], "2026-07-09")
            self.assertIn("PreCashComponent", detail.metadata["BackfillMissingFields"])

    def test_writer_rejects_mismatched_target_day(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = backfill_sh_pcf.BackfillPayload(
                metadata={"TradingDay": "2026-07-09"},
                components=(),
                source_url="https://example.test",
                source_kind="test",
                raw_files=(),
            )
            with self.assertRaises(backfill_sh_pcf.BackfillError):
                backfill_sh_pcf.write_payload_xml(
                    Path(temp_dir) / "513350.xml",
                    backfill_sh_pcf.SPEC_BY_CODE["513350"],
                    date(2026, 7, 10),
                    payload,
                )

    def test_sse_calendar_excludes_2026_holidays(self) -> None:
        days = set(backfill_sh_pcf.iter_sse_trading_days(date(2026, 4, 30), date(2026, 6, 22)))
        self.assertNotIn(date(2026, 5, 1), days)
        self.assertNotIn(date(2026, 5, 4), days)
        self.assertNotIn(date(2026, 5, 5), days)
        self.assertNotIn(date(2026, 6, 19), days)
        self.assertIn(date(2026, 5, 6), days)
        self.assertIn(date(2026, 6, 22), days)

    def test_public_nav_fallback_is_explicitly_partial(self) -> None:
        class FakeHttp:
            def request(self, _url: str, *, form=None) -> str:
                if form is not None:
                    raise AssertionError("public NAV fallback must use GET")
                return 'Data_netWorthTrend = [{"x":1783641600000,"y":1.2345}];'

        payload = backfill_sh_pcf.public_nav_payload(
            backfill_sh_pcf.SPEC_BY_CODE["513100"],
            date(2026, 7, 10),
            FakeHttp(),
        )
        self.assertEqual(payload.metadata["TradingDay"], "2026-07-10")
        self.assertEqual(payload.metadata["NAV"], "1.2345")
        self.assertEqual(payload.source_kind, "公开历史净值映射（非基金公司 PCF）")


if __name__ == "__main__":
    unittest.main()
