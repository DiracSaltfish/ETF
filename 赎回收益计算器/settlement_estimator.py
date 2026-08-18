from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import basket_calibration
import redemption_engine


PREDICTED_REFUND_SHARES_PER_CU = Decimal("996")
PREDICTED_REFUND_PRICE_WINDOW = "1559_close"
PREDICTED_BASKET_FX_QUOTE_TIME = "16:00"
PREDICTED_REFUND_SOURCE = (
    "总篮子资产：XOP 15:59一分钟收盘价 + "
    f"CFETS {PREDICTED_BASKET_FX_QUOTE_TIME} + PCF EstimateCashComponent"
)
PREDICTED_BASKET_MODEL_VERSION = "total_asset_996_1559_cfets1600_v1"
PREDICTED_REFUND_FIELDS = (
    "basket_id",
    "calculated_at",
    "redeem_day",
    "contract_no",
    "redeem_qty",
    "creation_redemption_unit",
    "unit_ratio",
    "shares_per_cu",
    "estimated_xop_shares",
    "price_window",
    "xop_price",
    "settlement_fx",
    "predicted_refund_cny",
    "predicted_cash_difference_cny",
    "predicted_basket_asset_cny",
    "model_version",
    "source",
)


@dataclass(frozen=True)
class RedemptionEstimate:
    basket_id: str
    redeem_day: date
    contract_no: int
    redeem_qty: int
    creation_redemption_unit: int
    unit_ratio: Decimal
    shares_per_cu: Decimal
    estimated_xop_shares: Decimal
    price_window: str
    xop_price: Decimal
    settlement_fx: Decimal
    estimated_refund_cny: Decimal
    estimated_cash_difference_cny: Decimal
    estimated_total_cash_cny: Decimal
    domestic_cost_cny: Decimal
    estimated_domestic_pnl_cny: Decimal
    confidence: str
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class DateRedemptionEstimate:
    redeem_day: date
    redeem_qty: int
    creation_redemption_unit: int
    unit_ratio: Decimal
    shares_per_cu: Decimal
    estimated_xop_shares: Decimal
    price_window: str
    xop_price: Decimal
    settlement_fx: Decimal
    estimated_refund_cny: Decimal
    estimated_cash_difference_cny: Decimal
    estimated_total_cash_cny: Decimal
    actual_refund_cny: Decimal | None = None
    actual_cash_difference_cny: Decimal | None = None
    actual_total_cash_cny: Decimal | None = None
    inferred_shares_per_cu: Decimal | None = None
    error_vs_calibration: Decimal | None = None


@dataclass(frozen=True)
class PredictedRefund:
    basket_id: str
    calculated_at: str
    redeem_day: date
    contract_no: int
    redeem_qty: int
    creation_redemption_unit: int
    unit_ratio: Decimal
    shares_per_cu: Decimal
    estimated_xop_shares: Decimal
    price_window: str
    xop_price: Decimal
    settlement_fx: Decimal
    predicted_refund_cny: Decimal
    predicted_cash_difference_cny: Decimal | None = None
    predicted_basket_asset_cny: Decimal | None = None
    model_version: str = ""
    source: str = PREDICTED_REFUND_SOURCE


class PredictedRefundStore:
    def __init__(self, csv_path: Path | str) -> None:
        self.csv_path = Path(csv_path).expanduser().resolve()

    def load(self) -> list[PredictedRefund]:
        if not self.csv_path.exists():
            return []
        result: list[PredictedRefund] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                result.append(
                    PredictedRefund(
                        basket_id=str(row["basket_id"]),
                        calculated_at=str(row["calculated_at"]),
                        redeem_day=date.fromisoformat(str(row["redeem_day"])),
                        contract_no=int(row["contract_no"]),
                        redeem_qty=int(row["redeem_qty"]),
                        creation_redemption_unit=int(row["creation_redemption_unit"]),
                        unit_ratio=Decimal(row["unit_ratio"]),
                        shares_per_cu=Decimal(row["shares_per_cu"]),
                        estimated_xop_shares=Decimal(row["estimated_xop_shares"]),
                        price_window=str(row["price_window"]),
                        xop_price=Decimal(row["xop_price"]),
                        settlement_fx=Decimal(row["settlement_fx"]),
                        predicted_refund_cny=Decimal(row["predicted_refund_cny"]),
                        predicted_cash_difference_cny=(
                            Decimal(row["predicted_cash_difference_cny"])
                            if str(row.get("predicted_cash_difference_cny") or "").strip()
                            else None
                        ),
                        predicted_basket_asset_cny=(
                            Decimal(row["predicted_basket_asset_cny"])
                            if str(row.get("predicted_basket_asset_cny") or "").strip()
                            else None
                        ),
                        model_version=str(row.get("model_version") or ""),
                        source=str(row.get("source") or PREDICTED_REFUND_SOURCE),
                    )
                )
        return sorted(result, key=lambda item: (item.redeem_day, item.contract_no, item.basket_id))

    def by_basket_id(self) -> dict[str, PredictedRefund]:
        return {item.basket_id: item for item in self.load()}

    def append_or_replace_many(self, predictions: list[PredictedRefund]) -> None:
        replaced_ids = {item.basket_id for item in predictions}
        items = [item for item in self.load() if item.basket_id not in replaced_ids]
        items.extend(predictions)
        items.sort(key=lambda item: (item.redeem_day, item.contract_no, item.basket_id))
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(PREDICTED_REFUND_FIELDS))
            writer.writeheader()
            for item in items:
                writer.writerow({field: getattr(item, field) for field in PREDICTED_REFUND_FIELDS})


def estimate_predicted_refund(
    basket: redemption_engine.BasketResult,
    xop_price: Decimal,
    settlement_fx: Decimal,
    *,
    calculated_at: str | None = None,
    shares_per_cu: Decimal = PREDICTED_REFUND_SHARES_PER_CU,
    price_window: str = PREDICTED_REFUND_PRICE_WINDOW,
    pcf_estimate_cash_component_cny: Decimal = Decimal("0"),
    creation_redemption_unit: int = redemption_engine.DEFAULT_REDEMPTION_UNIT,
) -> PredictedRefund:
    xop_price = Decimal(xop_price)
    settlement_fx = Decimal(settlement_fx)
    shares_per_cu = Decimal(shares_per_cu)
    pcf_estimate_cash_component_cny = Decimal(pcf_estimate_cash_component_cny)
    if creation_redemption_unit <= 0 or basket.redeem_qty <= 0:
        raise ValueError("赎回份额和最小申赎单位必须大于 0")
    if xop_price <= 0:
        raise ValueError("XOP 预测价格必须大于 0")
    if settlement_fx <= 0:
        raise ValueError("CFETS 结算汇率必须大于 0")
    if shares_per_cu <= 0:
        raise ValueError("每申赎单位XOP股数必须大于 0")
    unit_ratio = Decimal(basket.redeem_qty) / Decimal(creation_redemption_unit)
    estimated_xop_shares = shares_per_cu * unit_ratio
    predicted_refund = estimated_xop_shares * xop_price * settlement_fx
    predicted_cash_difference = pcf_estimate_cash_component_cny * unit_ratio
    predicted_basket_asset = predicted_refund + predicted_cash_difference
    return PredictedRefund(
        basket_id=basket.id,
        calculated_at=calculated_at or datetime.now().isoformat(timespec="seconds"),
        redeem_day=basket.redeem_day,
        contract_no=basket.contract_no,
        redeem_qty=basket.redeem_qty,
        creation_redemption_unit=creation_redemption_unit,
        unit_ratio=unit_ratio,
        shares_per_cu=shares_per_cu,
        estimated_xop_shares=estimated_xop_shares,
        price_window=price_window,
        xop_price=xop_price,
        settlement_fx=settlement_fx,
        predicted_refund_cny=redemption_engine.money(predicted_refund),
        predicted_cash_difference_cny=redemption_engine.money(predicted_cash_difference),
        predicted_basket_asset_cny=redemption_engine.money(predicted_basket_asset),
        model_version=PREDICTED_BASKET_MODEL_VERSION,
    )


def estimate_redemption_for_date(
    redeem_day: date,
    redeem_qty: int,
    calibration_state: basket_calibration.BasketCalibrationState,
    pcf_point: basket_calibration.PcfCalibrationPoint,
    xop_sell_price: Decimal,
    settlement_fx: Decimal,
    *,
    actual_refund_cny: Decimal | None = None,
    actual_cash_difference_cny: Decimal | None = None,
    price_window: str = "1540_1600",
) -> DateRedemptionEstimate:
    xop_sell_price = Decimal(xop_sell_price)
    settlement_fx = Decimal(settlement_fx)
    if redeem_qty <= 0 or pcf_point.creation_redemption_unit <= 0:
        raise ValueError("赎回份额和最小申赎单位必须大于 0")
    if xop_sell_price <= 0:
        raise ValueError("XOP 卖出价格必须大于 0")
    if settlement_fx <= 0:
        raise ValueError("CFETS 结算汇率必须大于 0")
    unit_ratio = Decimal(redeem_qty) / Decimal(pcf_point.creation_redemption_unit)
    estimated_shares = calibration_state.shares_per_cu * unit_ratio
    estimated_refund = estimated_shares * xop_sell_price * settlement_fx
    estimated_cash_difference = pcf_point.estimate_cash_component * unit_ratio
    estimated_total = estimated_refund + estimated_cash_difference

    actual_refund = Decimal(actual_refund_cny) if actual_refund_cny is not None else None
    actual_cash_difference = (
        Decimal(actual_cash_difference_cny) if actual_cash_difference_cny is not None else None
    )
    actual_total = None
    inferred_shares = None
    error_vs_calibration = None
    if actual_refund is not None:
        inferred_shares = actual_refund / settlement_fx / xop_sell_price / unit_ratio
        error_vs_calibration = inferred_shares - calibration_state.shares_per_cu
        actual_total = actual_refund + (actual_cash_difference or Decimal("0"))

    return DateRedemptionEstimate(
        redeem_day=redeem_day,
        redeem_qty=redeem_qty,
        creation_redemption_unit=pcf_point.creation_redemption_unit,
        unit_ratio=unit_ratio,
        shares_per_cu=calibration_state.shares_per_cu,
        estimated_xop_shares=estimated_shares,
        price_window=price_window,
        xop_price=xop_sell_price,
        settlement_fx=settlement_fx,
        estimated_refund_cny=redemption_engine.money(estimated_refund),
        estimated_cash_difference_cny=redemption_engine.money(estimated_cash_difference),
        estimated_total_cash_cny=redemption_engine.money(estimated_total),
        actual_refund_cny=redemption_engine.money(actual_refund) if actual_refund is not None else None,
        actual_cash_difference_cny=(
            redemption_engine.money(actual_cash_difference) if actual_cash_difference is not None else None
        ),
        actual_total_cash_cny=redemption_engine.money(actual_total) if actual_total is not None else None,
        inferred_shares_per_cu=inferred_shares,
        error_vs_calibration=error_vs_calibration,
    )


def estimate_redemption(
    basket: redemption_engine.BasketResult,
    calibration_state: basket_calibration.BasketCalibrationState,
    pcf_point: basket_calibration.PcfCalibrationPoint,
    xop_sell_price: Decimal,
    settlement_fx: Decimal,
    price_window: str = "1540_1600",
) -> RedemptionEstimate:
    xop_sell_price = Decimal(xop_sell_price)
    settlement_fx = Decimal(settlement_fx)
    if xop_sell_price <= 0:
        raise ValueError("XOP 卖出价格必须大于 0")
    if settlement_fx <= 0:
        raise ValueError("CFETS 结算汇率必须大于 0")
    if basket.redeem_qty <= 0 or pcf_point.creation_redemption_unit <= 0:
        raise ValueError("赎回份额和最小申赎单位必须大于 0")
    unit_ratio = Decimal(basket.redeem_qty) / Decimal(pcf_point.creation_redemption_unit)
    estimated_xop_shares = calibration_state.shares_per_cu * unit_ratio
    refund = estimated_xop_shares * xop_sell_price * settlement_fx
    has_actual_cash_difference = any(item.action == "ETF 现金差额" for item in basket.cash_flows)
    cash_difference = (
        basket.cash_difference
        if has_actual_cash_difference or basket.cash_difference != 0
        else pcf_point.estimate_cash_component * unit_ratio
    )
    total_cash = refund + cash_difference
    domestic_pnl = total_cash - basket.domestic_cost
    warnings = [item for item in (calibration_state.warning,) if item]
    if has_actual_cash_difference or basket.cash_difference != 0:
        warnings.append("现金差额已使用 QMT 实际值")
    if basket.refund_amount > 0:
        warnings.append(f"已有实际补券退款 {redemption_engine.money(basket.refund_amount)} 元，保留估算用于误差分析")
    return RedemptionEstimate(
        basket_id=basket.id,
        redeem_day=basket.redeem_day,
        contract_no=basket.contract_no,
        redeem_qty=basket.redeem_qty,
        creation_redemption_unit=pcf_point.creation_redemption_unit,
        unit_ratio=unit_ratio,
        shares_per_cu=calibration_state.shares_per_cu,
        estimated_xop_shares=estimated_xop_shares,
        price_window=price_window,
        xop_price=xop_sell_price,
        settlement_fx=settlement_fx,
        estimated_refund_cny=redemption_engine.money(refund),
        estimated_cash_difference_cny=redemption_engine.money(cash_difference),
        estimated_total_cash_cny=redemption_engine.money(total_cash),
        domestic_cost_cny=redemption_engine.money(basket.domestic_cost),
        estimated_domestic_pnl_cny=redemption_engine.money(domestic_pnl),
        confidence=calibration_state.confidence,
        warnings=tuple(warnings),
    )
