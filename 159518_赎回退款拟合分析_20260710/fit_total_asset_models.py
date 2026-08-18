#!/usr/bin/env python3
"""Fit 159518 at the *total fund asset* level: refund + cash difference.

This keeps PCF securities and cash separate: XOP-equivalent shares are derived
only from securities; PCF EstimateCashComponent is then added once to forecast
the basket's total domestic receipt.
"""

from __future__ import annotations

import csv
import math
import statistics
import sys
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT.parent / "赎回收益计算器"
OUT_DIR = ROOT / "output"
sys.path.insert(0, str(ROOT))
import fit_redemption_models as prior


def component_stock_inputs() -> tuple[list[dict[str, object]], int, dict[object, float]]:
    """Return stock-only XOP equivalents and PCF cash forecasts by redemption day."""
    sys.path.insert(0, str(SOURCE_ROOT))
    import szse_pcf
    from market_data import CsvXopPriceProvider

    closes = prior.read_component_closes()
    store = szse_pcf.SzsePcfStore(SOURCE_ROOT / "szse_pcf_cache")
    xop_prices = CsvXopPriceProvider(SOURCE_ROOT / "market_data" / "xop_prices.csv")
    rows: list[dict[str, object]] = []
    cash_estimate: dict[object, float] = {}
    for pcf_day in prior.PCF_DAYS:
        detail = store.ensure_fund_detail(pcf_day, "159518")
        components = [
            row for row in detail.components
            if row.get("UnderlyingSecurityIDSource") == "9999" and Decimal(row.get("ComponentShare", "0")) > 0
        ]
        missing = [row["UnderlyingSecurityID"] for row in components if (row["UnderlyingSecurityID"], pcf_day) not in closes]
        stock_usd = sum(
            float(Decimal(row["ComponentShare"])) * closes[(row["UnderlyingSecurityID"], pcf_day)]
            for row in components if (row["UnderlyingSecurityID"], pcf_day) in closes
        )
        xop_close = float(xop_prices.get_close(pcf_day))
        estimate_cash = float(Decimal(detail.metadata["EstimateCashComponent"]))
        cash_estimate[pcf_day] = estimate_cash
        rows.append(
            {
                "pcf_day": pcf_day.isoformat(),
                "component_count": len(components),
                "component_stock_value_usd": stock_usd,
                "xop_daily_close_usd": xop_close,
                "stock_only_xop_equivalent_shares": stock_usd / xop_close,
                "pcf_estimate_cash_component_cny": estimate_cash,
                "pcf_cash_component_cny": float(Decimal(detail.metadata.get("CashComponent", "0"))),
                "missing_component_count": len(missing),
                "missing_components": ",".join(missing),
            }
        )
    shares = int(round(statistics.median(float(row["stock_only_xop_equivalent_shares"]) for row in rows)))
    return rows, shares, cash_estimate


def calc_metrics(errors: list[float]) -> tuple[float, float, float]:
    return (
        math.sqrt(statistics.fmean(error * error for error in errors)),
        statistics.fmean(abs(error) for error in errors),
        max(abs(error) for error in errors),
    )


def fit_models(observations, shares: int, cash_estimate: dict, candidates, fx_data) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for fx_time, fx_by_day in fx_data.items():
        for offset in range(3):
            fx_dates = {item.redeem_day: prior.next_fx_days(fx_by_day, item.redeem_day)[offset] for item in observations}
            for candidate in candidates:
                factor = {item.redeem_day: candidate.price_by_day[item.redeem_day] * fx_by_day[fx_dates[item.redeem_day]] for item in observations}
                target_net_of_cash = {item.redeem_day: item.refund_cny + item.cash_difference_cny - cash_estimate[item.redeem_day] for item in observations}
                q_continuous = sum(target_net_of_cash[item.redeem_day] * factor[item.redeem_day] for item in observations) / sum(factor[item.redeem_day] ** 2 for item in observations)
                q_integer = int(math.floor(q_continuous + 0.5))

                def scored(q: int) -> tuple[float, float, float]:
                    errors = [q * factor[item.redeem_day] + cash_estimate[item.redeem_day] - (item.refund_cny + item.cash_difference_cny) for item in observations]
                    return calc_metrics(errors)

                fixed = scored(shares)
                auto = scored(q_integer)
                rows.append(
                    {
                        "price_model": candidate.name,
                        "price_method": candidate.method,
                        "price_window_ny": candidate.window,
                        "fx_time": fx_time,
                        "fx_offset": f"T+{offset}",
                        "stock_only_fixed_shares": shares,
                        "total_asset_rmse_cny": fixed[0],
                        "total_asset_mae_cny": fixed[1],
                        "total_asset_max_abs_cny": fixed[2],
                        "unconstrained_shares_continuous": q_continuous,
                        "unconstrained_shares_integer": q_integer,
                        "auto_total_asset_rmse_cny": auto[0],
                        "auto_total_asset_mae_cny": auto[1],
                    }
                )
    rows.sort(key=lambda row: float(row["total_asset_rmse_cny"]))
    return rows


def total_predictions(model: dict[str, object], observations, cash_estimate: dict, candidates, fx_data) -> list[dict[str, object]]:
    candidate = next(item for item in candidates if item.name == model["price_model"])
    fx_by_day = fx_data[model["fx_time"]]
    offset = int(str(model["fx_offset"]).removeprefix("T+"))
    shares = int(model["stock_only_fixed_shares"])
    result = []
    for item in observations:
        fx_day = prior.next_fx_days(fx_by_day, item.redeem_day)[offset]
        price = candidate.price_by_day[item.redeem_day]
        fx = fx_by_day[fx_day]
        refund = shares * price * fx
        predicted_total = refund + cash_estimate[item.redeem_day]
        actual_total = item.refund_cny + item.cash_difference_cny
        result.append(
            {
                "redeem_day": item.redeem_day.isoformat(),
                "actual_refund_cny": item.refund_cny,
                "actual_cash_difference_cny": item.cash_difference_cny,
                "actual_total_asset_cny": actual_total,
                "xop_price_usd": price,
                "fx_date": fx_day.isoformat(),
                "fx_usd_cny": fx,
                "predicted_refund_cny": refund,
                "pcf_estimate_cash_difference_cny": cash_estimate[item.redeem_day],
                "predicted_total_asset_cny": predicted_total,
                "predicted_minus_actual_total_cny": predicted_total - actual_total,
                "cash_forecast_minus_actual_cny": cash_estimate[item.redeem_day] - item.cash_difference_cny,
            }
        )
    return result


def main() -> int:
    observations = prior.load_observations()
    component_rows, stock_shares, cash_estimate = component_stock_inputs()
    candidates = prior.build_price_candidates()
    fx_data = prior.read_fx()
    ranking = fit_models(observations, stock_shares, cash_estimate, candidates, fx_data)
    best_overall = ranking[0]
    best_close_t0 = next(row for row in ranking if row["fx_time"] == "CLOSE" and row["fx_offset"] == "T+0")
    predictions = total_predictions(best_overall, observations, cash_estimate, candidates, fx_data)
    close_t0_predictions = total_predictions(best_close_t0, observations, cash_estimate, candidates, fx_data)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    prior.write_csv(OUT_DIR / "total_asset_component_inputs.csv", component_rows, list(component_rows[0]))
    prior.write_csv(OUT_DIR / "total_asset_model_ranking.csv", ranking, list(ranking[0]))
    prior.write_csv(OUT_DIR / "total_asset_best_predictions.csv", predictions, list(predictions[0]))
    prior.write_csv(OUT_DIR / "total_asset_close_t0_predictions.csv", close_t0_predictions, list(close_t0_predictions[0]))

    close_top = [row for row in ranking if row["fx_time"] == "CLOSE" and row["fx_offset"] == "T+0"][:12]
    lines = [
        "# 159518 总基金资产模型（退款＋现金差额）",
        "",
        "口径：`预测总资产 = 证券成分对应 XOP 股数 × XOP 价格 × 汇率 + PCF EstimateCashComponent`。",
        "",
        f"- PCF 成分券证券市值（不含现金差额）反推的 XOP 股数中位数取整：**{stock_shares} 股**。",
        f"- 固定 {stock_shares} 股、所有已获取汇率时点中最佳的总资产模型：**{best_overall['price_model']} × CFETS {best_overall['fx_time']} / {best_overall['fx_offset']}**，RMSE **{float(best_overall['total_asset_rmse_cny']):,.2f} CNY**，MAE **{float(best_overall['total_asset_mae_cny']):,.2f} CNY**。",
        f"- 若强制使用本地 `CLOSE` 字段，T 日最佳模型为 **{best_close_t0['price_model']}**，RMSE **{float(best_close_t0['total_asset_rmse_cny']):,.2f} CNY**。",
        "",
        "## CFETS CLOSE / T 日候选排名",
        "",
        "| 排名 | XOP 模型 | 总资产 RMSE | 总资产 MAE | 自动拟合股数 |",
        "|---:|---|---:|---:|---:|",
    ]
    lines.extend(
        f"| {index} | {row['price_model']} | {float(row['total_asset_rmse_cny']):,.2f} | {float(row['total_asset_mae_cny']):,.2f} | {float(row['unconstrained_shares_continuous']):.3f} |"
        for index, row in enumerate(close_top, start=1)
    )
    lines.extend(
        [
            "",
            "## 逐日总资产校验",
            "",
            "| 赎回日 | 实际退款 | 实际现金差额 | 实际总资产 | 预测退款 | PCF预计现金差额 | 预测总资产 | 总资产误差 |",
            "|---|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {row['redeem_day']} | {float(row['actual_refund_cny']):,.2f} | {float(row['actual_cash_difference_cny']):,.2f} | {float(row['actual_total_asset_cny']):,.2f} | {float(row['predicted_refund_cny']):,.2f} | {float(row['pcf_estimate_cash_difference_cny']):,.2f} | {float(row['predicted_total_asset_cny']):,.2f} | {float(row['predicted_minus_actual_total_cny']):,.2f} |"
        for row in predictions
    )
    lines.extend(
        [
            "",
            "说明：历史实际现金差额与 PCF 当日 EstimateCashComponent 存在偏差；这部分是现金差额预测误差，不能错误归因为 XOP 平仓时点。",
        ]
    )
    (OUT_DIR / "总基金资产拟合报告.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Stock-only PCF anchor: {stock_shares}; best total-asset model: "
        f"{best_overall['price_model']} / CFETS {best_overall['fx_time']} {best_overall['fx_offset']} / "
        f"RMSE {float(best_overall['total_asset_rmse_cny']):,.2f} CNY"
    )


if __name__ == "__main__":
    main()
