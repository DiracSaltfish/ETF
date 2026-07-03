from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date, datetime
from urllib.error import HTTPError

import szse_pcf


SAMPLE_DAY = date(2026, 7, 3)
SAMPLE_HTML = (
    "<a href='javascript:void(0);' encode-open='/files/text/etf/ETF15951820260703.txt' target='_blank'>"
    "ETF159518申购赎回清单(2026-07-03)</a>&nbsp;&nbsp;"
    "<a style='cursor:pointer' "
    "href=/modules/report/views/eft_download_new.html?path=%2Ffiles%2Ftext%2FETFDown%2F&"
    "filename=pcf_159518_20260703%3B159518ETF20260703&opencode=ETF15951820260703.txt "
    "target='_blank'><img border='0'></a>"
)
SAMPLE_LIST_JSON = json.dumps(
    [
        {
            "metadata": {
                "catalogid": "sgshqd",
                "tabkey": "tab1",
                "pagecount": 1,
                "recordcount": 1,
            },
            "data": [{"jjdm": SAMPLE_HTML}],
            "error": None,
        }
    ],
    ensure_ascii=False,
).encode("utf-8")
SAMPLE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<PCFFile xmlns="http://ts.szse.cn/Fund">
  <Version>1.0</Version>
  <SecurityID>159518</SecurityID>
  <Symbol>标普油气ETF嘉实</Symbol>
  <FundManagementCompany>嘉实基金管理有限公司</FundManagementCompany>
  <UnderlyingSecurityID>SPSIOP</UnderlyingSecurityID>
  <CreationRedemptionUnit>1000000.00</CreationRedemptionUnit>
  <EstimateCashComponent>1638.95</EstimateCashComponent>
  <TradingDay>20260703</TradingDay>
  <PreTradingDay>20260701</PreTradingDay>
  <CashComponent>1411.95</CashComponent>
  <NAVperCU>1043484.98</NAVperCU>
  <NAV>1.0435</NAV>
  <Publish>Y</Publish>
  <Creation>N</Creation>
  <Redemption>N</Redemption>
  <RecordNum>1</RecordNum>
  <TotalRecordNum>52</TotalRecordNum>
  <Components>
    <Component>
      <UnderlyingSecurityID>159900</UnderlyingSecurityID>
      <UnderlyingSymbol>申赎现金</UnderlyingSymbol>
      <ComponentShare>0.00</ComponentShare>
      <SubstituteFlag>2</SubstituteFlag>
      <PremiumRatio>0.00000</PremiumRatio>
      <CreationCashSubstitute>1146030.6400</CreationCashSubstitute>
      <RedemptionCashSubstitute>0.0000</RedemptionCashSubstitute>
    </Component>
    <Component>
      <UnderlyingSecurityID>APA</UnderlyingSecurityID>
      <UnderlyingSymbol>APA</UnderlyingSymbol>
      <ComponentShare>110.00</ComponentShare>
      <SubstituteFlag>1</SubstituteFlag>
      <PremiumRatio>0.10000</PremiumRatio>
      <CreationCashSubstitute>0.0000</CreationCashSubstitute>
      <RedemptionCashSubstitute>0.0000</RedemptionCashSubstitute>
    </Component>
  </Components>
</PCFFile>
""".encode("utf-8")
SAMPLE_TXT = """标普油气ETF嘉实申购赎回清单
( 2026-07-03 )
基金代码：159518
""".encode("gb18030")


class SzsePcfStoreTest(unittest.TestCase):
    def test_day_index_is_fetched_and_cached(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            if "ShowReport/data" not in url:
                raise AssertionError(f"unexpected url: {url}")
            return SAMPLE_LIST_JSON

        with tempfile.TemporaryDirectory() as temp_dir:
            store = szse_pcf.SzsePcfStore(temp_dir, fetch_bytes=fetcher, min_request_interval_seconds=0)
            index = store.ensure_day_index(SAMPLE_DAY)
            self.assertEqual(len(index.items), 1)
            item = index.items[0]
            self.assertEqual(item.fund_code, "159518")
            self.assertEqual(item.opencode_url, "https://reportdocs.static.szse.cn/files/text/etf/ETF15951820260703.txt")
            self.assertTrue(item.xml_candidate_urls[0].endswith("/files/text/ETFDown/pcf_159518_20260703.xml"))
            self.assertTrue(store.index_path(SAMPLE_DAY).exists())

            calls.clear()
            cached = store.ensure_day_index(SAMPLE_DAY)
            self.assertEqual(len(cached.items), 1)
            self.assertEqual(calls, [])

    def test_detail_uses_local_cache_before_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = szse_pcf.SzsePcfStore(temp_dir, fetch_bytes=lambda _url: (_ for _ in ()).throw(AssertionError("network not expected")))
            index = szse_pcf.PcfDayIndex(
                trade_date=SAMPLE_DAY,
                fetched_at="2026-07-03T20:00:00",
                source_page_url=szse_pcf.LIST_PAGE_URL,
                source_api_url="https://example.invalid",
                record_count=1,
                page_count=1,
                items=(store._parse_list_item(SAMPLE_HTML, SAMPLE_DAY),),
            )
            store.save_day_index(index)
            day_dir = store.day_dir(SAMPLE_DAY)
            (day_dir / "xml").mkdir(parents=True, exist_ok=True)
            (day_dir / "txt").mkdir(parents=True, exist_ok=True)
            (day_dir / "xml" / "159518.xml").write_bytes(SAMPLE_XML)
            (day_dir / "txt" / "159518.txt").write_text(SAMPLE_TXT.decode("gb18030"), encoding="utf-8")

            detail = store.ensure_detail(SAMPLE_DAY, "159518")
            self.assertEqual(detail.fund_name, "标普油气ETF嘉实")
            self.assertEqual(len(detail.components), 2)
            self.assertIn("基金代码：159518", detail.raw_text)

    def test_detail_fetches_and_persists_xml_and_txt(self) -> None:
        calls: list[str] = []

        with tempfile.TemporaryDirectory() as temp_dir:
            probe_store = szse_pcf.SzsePcfStore(temp_dir)
            item = probe_store._parse_list_item(SAMPLE_HTML, SAMPLE_DAY)
            index = szse_pcf.PcfDayIndex(
                trade_date=SAMPLE_DAY,
                fetched_at="2026-07-03T20:00:00",
                source_page_url=szse_pcf.LIST_PAGE_URL,
                source_api_url="https://example.invalid",
                record_count=1,
                page_count=1,
                items=(item,),
            )

            def fetcher(url: str) -> bytes:
                calls.append(url)
                if url == item.xml_candidate_urls[0]:
                    return SAMPLE_XML
                if url == item.opencode_url:
                    return SAMPLE_TXT
                raise AssertionError(f"unexpected url: {url}")

            store = szse_pcf.SzsePcfStore(temp_dir, fetch_bytes=fetcher, min_request_interval_seconds=0)
            store.save_day_index(index)
            detail = store.ensure_detail(SAMPLE_DAY, "159518")

            self.assertEqual(detail.metadata["SecurityID"], "159518")
            self.assertEqual(detail.components[0]["UnderlyingSecurityID"], "159900")
            self.assertTrue((store.day_dir(SAMPLE_DAY) / "xml" / "159518.xml").exists())
            self.assertTrue((store.day_dir(SAMPLE_DAY) / "txt" / "159518.txt").exists())
            self.assertEqual(len(calls), 2)

    def test_target_day_index_is_generated_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            store = szse_pcf.SzsePcfStore(
                temp_dir,
                fetch_bytes=lambda _url: (_ for _ in ()).throw(AssertionError("network not expected")),
            )
            index = store.ensure_target_day_index(SAMPLE_DAY)
            self.assertEqual(len(index.items), 1)
            self.assertEqual(index.items[0].fund_code, szse_pcf.TARGET_FUND_CODE)
            self.assertEqual(index.source_api_url, "")
            self.assertTrue(store.index_path(SAMPLE_DAY).exists())

    def test_target_detail_prefers_single_xml_request(self) -> None:
        calls: list[str] = []

        def fetcher(url: str) -> bytes:
            calls.append(url)
            if url.endswith("/pcf_159518_20260703.xml"):
                return SAMPLE_XML
            raise AssertionError(f"unexpected url: {url}")

        with tempfile.TemporaryDirectory() as temp_dir:
            store = szse_pcf.SzsePcfStore(temp_dir, fetch_bytes=fetcher, min_request_interval_seconds=0)
            detail = store.ensure_target_detail(SAMPLE_DAY)
            self.assertEqual(detail.metadata["SecurityID"], "159518")
            self.assertEqual(detail.raw_text, "")
            self.assertTrue((store.day_dir(SAMPLE_DAY) / "xml" / "159518.xml").exists())
            self.assertEqual(len(calls), 1)

    def test_rate_limit_cooldown_is_persisted(self) -> None:
        calls: list[str] = []
        current_time = [datetime(2026, 7, 3, 10, 0, 0)]

        def fetcher(url: str) -> bytes:
            calls.append(url)
            raise HTTPError(url, 429, "Too Many Requests", hdrs=None, fp=None)

        with tempfile.TemporaryDirectory() as temp_dir:
            store = szse_pcf.SzsePcfStore(
                temp_dir,
                fetch_bytes=fetcher,
                min_request_interval_seconds=0,
                now_fn=lambda: current_time[0],
                sleep_fn=lambda _seconds: None,
            )
            with self.assertRaises(szse_pcf.SzsePcfError) as first_error:
                store.ensure_target_detail(SAMPLE_DAY)
            self.assertIn("429", str(first_error.exception))
            first_call_count = len(calls)
            state = store.load_request_state()
            self.assertTrue(state.blocked_until)

            with self.assertRaises(szse_pcf.SzsePcfError) as second_error:
                store.ensure_target_detail(SAMPLE_DAY)
            self.assertIn("冷却期", str(second_error.exception))
            self.assertEqual(len(calls), first_call_count)


if __name__ == "__main__":
    unittest.main()
