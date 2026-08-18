from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import redemption_engine
import szse_pcf


PCF_FIELDS = (
    "pcf_trading_day",
    "valuation_day",
    "creation_redemption_unit",
    "nav_per_cu",
    "cash_component",
    "estimate_cash_component",
    "safe_mid_fx",
    "xop_close",
    "q_nav",
    "q_net",
    "chosen_q",
    "chosen_method",
    "warning",
)

OBSERVATION_FIELDS = (
    "basket_id",
    "redeem_day",
    "contract_no",
    "redeem_qty",
    "creation_redemption_unit",
    "unit_ratio",
    "actual_refund_cny",
    "actual_cash_difference_cny",
    "settlement_fx",
    "xop_price_proxy",
    "inferred_shares_per_cu",
    "pcf_q_net",
    "pcf_q_nav",
    "error_vs_q_net",
    "included",
    "warning",
)


@dataclass(frozen=True)
class PcfCalibrationPoint:
    pcf_trading_day: date
    valuation_day: date
    creation_redemption_unit: int
    nav_per_cu: Decimal
    cash_component: Decimal
    estimate_cash_component: Decimal
    safe_mid_fx: Decimal
    xop_close: Decimal
    q_nav: Decimal
    q_net: Decimal
    chosen_q: Decimal
    chosen_method: str
    warning: str = ""


@dataclass(frozen=True)
class BasketCalibrationState:
    trade_day: date
    shares_per_cu: Decimal
    method: str
    confidence: str
    sample_count: int
    cash_residual_cny: Decimal = Decimal("0")
    warning: str = ""


@dataclass(frozen=True)
class SettlementObservation:
    basket_id: str
    redeem_day: date
    contract_no: int
    redeem_qty: int
    creation_redemption_unit: int
    unit_ratio: Decimal
    actual_refund_cny: Decimal
    actual_cash_difference_cny: Decimal
    settlement_fx: Decimal
    xop_price_proxy: Decimal
    inferred_shares_per_cu: Decimal
    pcf_q_net: Decimal
    pcf_q_nav: Decimal
    error_vs_q_net: Decimal
    included: bool
    warning: str = ""


def _parse_day(value: str, field: str) -> date:
    text = str(value or "").strip()
    for pattern in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(text, pattern).date()
        except ValueError:
            pass
    raise ValueError(f"PCF {field} 不是有效日期: {text or '空'}")


def _decimal(value: object, field: str, *, default: Decimal | None = None) -> Decimal:
    text = str(value or "").replace(",", "").strip()
    if not text and default is not None:
        return default
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"PCF {field} 不是有效数字: {text or '空'}") from exc


def build_pcf_calibration_point(
    detail: szse_pcf.PcfDetail,
    safe_mid_fx: Decimal,
    xop_close: Decimal,
) -> PcfCalibrationPoint:
    safe_mid_fx = Decimal(safe_mid_fx)
    xop_close = Decimal(xop_close)
    if safe_mid_fx <= 0:
        raise ValueError("SAFE 人民币中间价必须大于 0")
    if xop_close <= 0:
        raise ValueError("XOP 收盘价必须大于 0")
    metadata = detail.metadata
    nav_per_cu = _decimal(metadata.get("NAVperCU"), "NAVperCU")
    cash_component = _decimal(metadata.get("CashComponent"), "CashComponent", default=Decimal("0"))
    estimate_cash_component = _decimal(
        metadata.get("EstimateCashComponent"), "EstimateCashComponent", default=Decimal("0")
    )
    unit_decimal = _decimal(metadata.get("CreationRedemptionUnit"), "CreationRedemptionUnit")
    if unit_decimal <= 0 or unit_decimal != unit_decimal.to_integral_value():
        raise ValueError("PCF CreationRedemptionUnit 必须是正整数")
    q_nav = nav_per_cu / safe_mid_fx / xop_close
    q_net = (nav_per_cu - cash_component) / safe_mid_fx / xop_close
    warnings: list[str] = []
    if q_net < Decimal("980") or q_net > Decimal("1010"):
        warnings.append(f"q_net {q_net:.4f} 超出 980–1010 合理区间")
    if abs(q_nav - q_net) > Decimal("2"):
        warnings.append(f"现金差额影响较大，q_nav 与 q_net 相差 {abs(q_nav - q_net):.4f} 股")
    return PcfCalibrationPoint(
        pcf_trading_day=_parse_day(metadata.get("TradingDay") or detail.trading_day, "TradingDay"),
        valuation_day=_parse_day(metadata.get("PreTradingDay") or "", "PreTradingDay"),
        creation_redemption_unit=int(unit_decimal),
        nav_per_cu=nav_per_cu,
        cash_component=cash_component,
        estimate_cash_component=estimate_cash_component,
        safe_mid_fx=safe_mid_fx,
        xop_close=xop_close,
        q_nav=q_nav,
        q_net=q_net,
        chosen_q=q_net,
        chosen_method="pcf_net",
        warning="；".join(warnings),
    )


def calibration_confidence(point: PcfCalibrationPoint) -> str:
    if Decimal("990") <= point.q_net <= Decimal("1000") and not point.warning:
        return "high"
    if Decimal("980") <= point.q_net <= Decimal("1010"):
        return "medium"
    return "low"


class CalibrationStore:
    def __init__(self, csv_path: Path | str) -> None:
        self.csv_path = Path(csv_path).expanduser().resolve()

    def append_or_replace_pcf_point(self, point: PcfCalibrationPoint) -> None:
        points = [item for item in self.load_pcf_points() if item.pcf_trading_day != point.pcf_trading_day]
        points.append(point)
        points.sort(key=lambda item: (item.pcf_trading_day, item.valuation_day))
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(PCF_FIELDS))
            writer.writeheader()
            for item in points:
                writer.writerow({field: getattr(item, field) for field in PCF_FIELDS})

    def load_pcf_points(self) -> list[PcfCalibrationPoint]:
        if not self.csv_path.exists():
            return []
        result: list[PcfCalibrationPoint] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                result.append(
                    PcfCalibrationPoint(
                        pcf_trading_day=date.fromisoformat(row["pcf_trading_day"]),
                        valuation_day=date.fromisoformat(row["valuation_day"]),
                        creation_redemption_unit=int(row["creation_redemption_unit"]),
                        nav_per_cu=Decimal(row["nav_per_cu"]),
                        cash_component=Decimal(row["cash_component"]),
                        estimate_cash_component=Decimal(row["estimate_cash_component"]),
                        safe_mid_fx=Decimal(row["safe_mid_fx"]),
                        xop_close=Decimal(row["xop_close"]),
                        q_nav=Decimal(row["q_nav"]),
                        q_net=Decimal(row["q_net"]),
                        chosen_q=Decimal(row["chosen_q"]),
                        chosen_method=row["chosen_method"],
                        warning=row.get("warning") or "",
                    )
                )
        return sorted(result, key=lambda item: (item.pcf_trading_day, item.valuation_day))

    def latest_point_for_day(self, trade_day: date) -> PcfCalibrationPoint | None:
        candidates = [item for item in self.load_pcf_points() if item.pcf_trading_day <= trade_day]
        return candidates[-1] if candidates else None

    def point_for_day(self, trade_day: date) -> PcfCalibrationPoint | None:
        return next((item for item in self.load_pcf_points() if item.pcf_trading_day == trade_day), None)

    def latest_state_for_day(self, trade_day: date) -> BasketCalibrationState | None:
        candidates = [item for item in self.load_pcf_points() if item.pcf_trading_day <= trade_day]
        if not candidates:
            return None
        point = candidates[-1]
        return BasketCalibrationState(
            trade_day=trade_day,
            shares_per_cu=point.chosen_q,
            method=point.chosen_method,
            confidence=calibration_confidence(point),
            sample_count=len(candidates),
            warning=point.warning,
        )


def build_settlement_observation(
    basket: redemption_engine.BasketResult,
    pcf_point: PcfCalibrationPoint,
    settlement_fx: Decimal,
    xop_price_proxy: Decimal,
) -> SettlementObservation | None:
    if basket.refund_amount <= 0:
        return None
    settlement_fx = Decimal(settlement_fx)
    xop_price_proxy = Decimal(xop_price_proxy)
    if settlement_fx <= 0:
        raise ValueError("CFETS 结算汇率必须大于 0")
    if xop_price_proxy <= 0:
        raise ValueError("XOP 价格代理必须大于 0")
    if pcf_point.creation_redemption_unit <= 0 or basket.redeem_qty <= 0:
        raise ValueError("赎回份额和最小申赎单位必须大于 0")
    unit_ratio = Decimal(basket.redeem_qty) / Decimal(pcf_point.creation_redemption_unit)
    inferred = basket.refund_amount / settlement_fx / xop_price_proxy / unit_ratio
    included = Decimal("980") <= inferred <= Decimal("1010")
    warning = "" if included else f"反推股数 {inferred:.4f} 超出 980–1010，不纳入校准"
    return SettlementObservation(
        basket_id=basket.id,
        redeem_day=basket.redeem_day,
        contract_no=basket.contract_no,
        redeem_qty=basket.redeem_qty,
        creation_redemption_unit=pcf_point.creation_redemption_unit,
        unit_ratio=unit_ratio,
        actual_refund_cny=basket.refund_amount,
        actual_cash_difference_cny=basket.cash_difference,
        settlement_fx=settlement_fx,
        xop_price_proxy=xop_price_proxy,
        inferred_shares_per_cu=inferred,
        pcf_q_net=pcf_point.q_net,
        pcf_q_nav=pcf_point.q_nav,
        error_vs_q_net=inferred - pcf_point.q_net,
        included=included,
        warning=warning,
    )


class SettlementObservationStore:
    def __init__(self, csv_path: Path | str) -> None:
        self.csv_path = Path(csv_path).expanduser().resolve()

    def append_or_replace(self, observation: SettlementObservation) -> None:
        items = [item for item in self.load() if item.basket_id != observation.basket_id]
        items.append(observation)
        items.sort(key=lambda item: (item.redeem_day, item.contract_no, item.basket_id))
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(OBSERVATION_FIELDS))
            writer.writeheader()
            for item in items:
                row = {field: getattr(item, field) for field in OBSERVATION_FIELDS}
                row["included"] = "1" if item.included else "0"
                writer.writerow(row)

    def load(self) -> list[SettlementObservation]:
        if not self.csv_path.exists():
            return []
        result: list[SettlementObservation] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                result.append(
                    SettlementObservation(
                        basket_id=row["basket_id"],
                        redeem_day=date.fromisoformat(row["redeem_day"]),
                        contract_no=int(row["contract_no"]),
                        redeem_qty=int(row["redeem_qty"]),
                        creation_redemption_unit=int(row["creation_redemption_unit"]),
                        unit_ratio=Decimal(row["unit_ratio"]),
                        actual_refund_cny=Decimal(row["actual_refund_cny"]),
                        actual_cash_difference_cny=Decimal(row["actual_cash_difference_cny"]),
                        settlement_fx=Decimal(row["settlement_fx"]),
                        xop_price_proxy=Decimal(row["xop_price_proxy"]),
                        inferred_shares_per_cu=Decimal(row["inferred_shares_per_cu"]),
                        pcf_q_net=Decimal(row["pcf_q_net"]),
                        pcf_q_nav=Decimal(row["pcf_q_nav"]),
                        error_vs_q_net=Decimal(row["error_vs_q_net"]),
                        included=str(row["included"]).lower() in {"1", "true", "yes"},
                        warning=row.get("warning") or "",
                    )
                )
        return sorted(result, key=lambda item: (item.redeem_day, item.contract_no, item.basket_id))
