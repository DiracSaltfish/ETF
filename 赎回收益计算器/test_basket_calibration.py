from __future__ import annotations

import tempfile
import unittest
from datetime import date
from decimal import Decimal
from pathlib import Path

import basket_calibration as calibration
import redemption_engine as engine
import szse_pcf


def pcf_detail(nav: str = "1043484.98", cash: str = "1411.95") -> szse_pcf.PcfDetail:
    item = szse_pcf.PcfListItem(
        fund_code="159518",
        trade_date=date(2026, 7, 3),
        title="sample",
        page_label="sample",
        opencode_name="",
        opencode_path="",
        opencode_url="",
        download_page_url="",
        xml_candidate_urls=(),
        cache_xml_path="",
        cache_txt_path="",
    )
    return szse_pcf.PcfDetail(
        item=item,
        metadata={
            "TradingDay": "20260703",
            "PreTradingDay": "20260701",
            "CreationRedemptionUnit": "1000000.00",
            "NAVperCU": nav,
            "CashComponent": cash,
            "EstimateCashComponent": "1638.95",
        },
        components=(),
        xml_path=None,
        txt_path=None,
        raw_text="",
    )


class BasketCalibrationTest(unittest.TestCase):
    def test_pcf_formulas_and_store(self) -> None:
        safe_mid = Decimal("6.8047")
        close = Decimal("154.80")
        point = calibration.build_pcf_calibration_point(pcf_detail(), safe_mid, close)
        self.assertEqual(point.q_nav, Decimal("1043484.98") / safe_mid / close)
        self.assertEqual(point.q_net, (Decimal("1043484.98") - Decimal("1411.95")) / safe_mid / close)
        self.assertEqual(point.chosen_q, point.q_net)
        self.assertEqual(point.chosen_method, "pcf_net")
        with tempfile.TemporaryDirectory() as temp_dir:
            store = calibration.CalibrationStore(Path(temp_dir) / "points.csv")
            store.append_or_replace_pcf_point(point)
            state = store.latest_state_for_day(date(2026, 7, 4))
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(state.shares_per_cu, point.q_net)

    def test_outlier_generates_warning(self) -> None:
        point = calibration.build_pcf_calibration_point(
            pcf_detail(nav="1500000", cash="0"), Decimal("6.8"), Decimal("158")
        )
        self.assertIn("980–1010", point.warning)

    def test_actual_refund_builds_observation(self) -> None:
        point = calibration.build_pcf_calibration_point(pcf_detail(), Decimal("6.8047"), Decimal("154.80"))
        basket = engine.BasketResult(
            id="basket-1",
            sequence=1,
            source="QMT1",
            redeem_day=date(2026, 7, 3),
            contract_no=1,
            redeem_qty=1_000_000,
            refund_amount=Decimal("1071240"),
        )
        observation = calibration.build_settlement_observation(
            basket, point, Decimal("6.78"), Decimal("158")
        )
        self.assertIsNotNone(observation)
        assert observation is not None
        self.assertEqual(observation.inferred_shares_per_cu, Decimal("1000"))
        self.assertTrue(observation.included)


if __name__ == "__main__":
    unittest.main()
