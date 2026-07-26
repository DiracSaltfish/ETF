from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

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
