from __future__ import annotations

import unittest
import tempfile
from datetime import date
from decimal import Decimal
from pathlib import Path

import basket_calibration as calibration
import redemption_engine as engine
from settlement_estimator import (
    PredictedRefundStore,
    estimate_predicted_refund,
    estimate_redemption,
    estimate_redemption_for_date,
)


class SettlementEstimatorTest(unittest.TestCase):
    def test_estimate_formulas(self) -> None:
        basket = engine.BasketResult(
            id="basket-1",
            sequence=1,
            source="QMT1",
            redeem_day=date(2026, 7, 3),
            contract_no=123,
            redeem_qty=1_000_000,
            domestic_cost=Decimal("1020000"),
        )
        point = calibration.PcfCalibrationPoint(
            pcf_trading_day=date(2026, 7, 3),
            valuation_day=date(2026, 7, 1),
            creation_redemption_unit=1_000_000,
            nav_per_cu=Decimal("0"),
            cash_component=Decimal("0"),
            estimate_cash_component=Decimal("1600"),
            safe_mid_fx=Decimal("6.8"),
            xop_close=Decimal("158"),
            q_nav=Decimal("995"),
            q_net=Decimal("995"),
            chosen_q=Decimal("995"),
            chosen_method="pcf_net",
        )
        state = calibration.BasketCalibrationState(
            trade_day=basket.redeem_day,
            shares_per_cu=Decimal("995"),
            method="pcf_net",
            confidence="high",
            sample_count=1,
        )
        result = estimate_redemption(basket, state, point, Decimal("158"), Decimal("6.78"))
        refund = Decimal("995") * Decimal("158") * Decimal("6.78")
        self.assertEqual(result.estimated_xop_shares, Decimal("995"))
        self.assertEqual(result.estimated_refund_cny, engine.money(refund))
        self.assertEqual(result.estimated_total_cash_cny, engine.money(refund + Decimal("1600")))
        self.assertEqual(result.estimated_domestic_pnl_cny, engine.money(refund + Decimal("1600") - Decimal("1020000")))

    def test_date_estimate_does_not_require_qmt_basket(self) -> None:
        point = calibration.PcfCalibrationPoint(
            pcf_trading_day=date(2026, 7, 3),
            valuation_day=date(2026, 7, 1),
            creation_redemption_unit=1_000_000,
            nav_per_cu=Decimal("0"),
            cash_component=Decimal("0"),
            estimate_cash_component=Decimal("1600"),
            safe_mid_fx=Decimal("6.8"),
            xop_close=Decimal("158"),
            q_nav=Decimal("995"),
            q_net=Decimal("995"),
            chosen_q=Decimal("995"),
            chosen_method="pcf_net",
        )
        state = calibration.BasketCalibrationState(
            trade_day=date(2026, 7, 3),
            shares_per_cu=Decimal("995"),
            method="pcf_net",
            confidence="high",
            sample_count=1,
        )
        result = estimate_redemption_for_date(
            date(2026, 7, 3),
            2_000_000,
            state,
            point,
            Decimal("158"),
            Decimal("6.78"),
            actual_refund_cny=Decimal("2142480"),
            actual_cash_difference_cny=Decimal("3200"),
        )
        self.assertEqual(result.estimated_xop_shares, Decimal("1990"))
        self.assertEqual(result.inferred_shares_per_cu, Decimal("1000"))
        self.assertEqual(result.error_vs_calibration, Decimal("5"))
        self.assertEqual(result.actual_total_cash_cny, Decimal("2145680.00"))

    def test_predicted_basket_asset_uses_total_asset_model_and_persists(self) -> None:
        basket = engine.BasketResult(
            id="basket-1",
            sequence=1,
            source="QMT1",
            redeem_day=date(2026, 7, 3),
            contract_no=123,
            redeem_qty=1_000_000,
        )
        prediction = estimate_predicted_refund(
            basket,
            Decimal("154"),
            Decimal("6.8"),
            calculated_at="2026-07-04T10:00:00",
            pcf_estimate_cash_component_cny=Decimal("1500"),
        )
        self.assertEqual(prediction.shares_per_cu, Decimal("996"))
        self.assertEqual(prediction.estimated_xop_shares, Decimal("996"))
        self.assertEqual(prediction.price_window, "1559_close")
        self.assertEqual(prediction.predicted_refund_cny, Decimal("1043011.20"))
        self.assertEqual(prediction.predicted_cash_difference_cny, Decimal("1500.00"))
        self.assertEqual(prediction.predicted_basket_asset_cny, Decimal("1044511.20"))

        with tempfile.TemporaryDirectory() as temp_dir:
            store = PredictedRefundStore(Path(temp_dir) / "predicted.csv")
            store.append_or_replace_many([prediction])
            loaded = store.by_basket_id()["basket-1"]

        self.assertEqual(loaded.predicted_refund_cny, Decimal("1043011.20"))
        self.assertEqual(loaded.predicted_basket_asset_cny, Decimal("1044511.20"))
        self.assertEqual(loaded.model_version, prediction.model_version)
        self.assertEqual(loaded.source, prediction.source)


if __name__ == "__main__":
    unittest.main()
