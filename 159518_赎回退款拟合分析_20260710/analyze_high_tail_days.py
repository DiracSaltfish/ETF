#!/usr/bin/env python3
"""Stress-test the total-asset model on the largest XOP tail moves only."""

from __future__ import annotations

import csv
import math
import statistics
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT.parent / "赎回收益计算器"
DATA = ROOT / "data" / "xop_tail_1m.csv"
OUT = ROOT / "output"
SHARES = 996.0
MOVE_THRESHOLD = 0.40  # USD, |15:59 close - 15:30 close|


def rmse(values: list[float]) -> float:
    return math.sqrt(statistics.fmean(value * value for value in values))


def main() -> int:
    with (OUT / "total_asset_best_predictions.csv").open(encoding="utf-8-sig", newline="") as handle:
        predictions = {row["redeem_day"]: row for row in csv.DictReader(handle)}
    by_day: dict[str, list[dict[str, str]]] = defaultdict(list)
    with DATA.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            by_day[row["trade_day"]].append(row)
    for rows in by_day.values():
        rows.sort(key=lambda row: row["minute"])

    inverse_rows: list[dict[str, object]] = []
    for day, prediction in sorted(predictions.items()):
        rows = by_day[day]
        start = float(rows[0]["close"])
        last = float(rows[-1]["close"])
        actual_total = float(prediction["actual_total_asset_cny"])
        cash_est = float(prediction["pcf_estimate_cash_difference_cny"])
        fx = float(prediction["fx_usd_cny"])
        implied = (actual_total - cash_est) / (SHARES * fx)
        nearest = min(rows, key=lambda row: abs(float(row["close"]) - implied))
        model_price = float(prediction["xop_price_usd"])
        inverse_rows.append(
            {
                "redeem_day": day,
                "tail_start_1530_usd": start,
                "tail_last_1559_usd": last,
                "tail_change_usd": last - start,
                "tail_range_usd": max(float(row["high"]) for row in rows) - min(float(row["low"]) for row in rows),
                "actual_total_asset_cny": actual_total,
                "pcf_estimate_cash_cny": cash_est,
                "implied_xop_price_usd": implied,
                "nearest_minute": nearest["minute"],
                "nearest_minute_close_usd": float(nearest["close"]),
                "nearest_minute_gap_usd": float(nearest["close"]) - implied,
                "selected_1559_price_usd": model_price,
                "selected_1559_gap_usd": model_price - implied,
                "is_large_tail_move": abs(last - start) >= MOVE_THRESHOLD,
            }
        )

    large = [row for row in inverse_rows if row["is_large_tail_move"]]
    candidate_rows: list[dict[str, object]] = []
    for minute_index in range(30):
        minute = by_day[next(iter(by_day))][minute_index]["minute"]
        errors = []
        for row in large:
            p = predictions[str(row["redeem_day"])]
            price = float(by_day[str(row["redeem_day"])][minute_index]["close"])
            estimate = SHARES * price * float(p["fx_usd_cny"]) + float(p["pcf_estimate_cash_difference_cny"])
            errors.append(estimate - float(p["actual_total_asset_cny"]))
        candidate_rows.append(
            {
                "model": f"点价_{minute}_1分钟收盘",
                "window": minute,
                "large_tail_day_count": len(large),
                "large_tail_rmse_cny": rmse(errors),
                "large_tail_mae_cny": statistics.fmean(abs(error) for error in errors),
            }
        )

    for start, end, label in ((24, 27, "15:54-15:57（3分钟TWAP）"), (25, 27, "15:55-15:57（2分钟TWAP）"), (25, 30, "15:55-16:00（5分钟TWAP）")):
        errors = []
        for row in large:
            p = predictions[str(row["redeem_day"])]
            price = statistics.fmean(float(item["close"]) for item in by_day[str(row["redeem_day"])][start:end])
            estimate = SHARES * price * float(p["fx_usd_cny"]) + float(p["pcf_estimate_cash_difference_cny"])
            errors.append(estimate - float(p["actual_total_asset_cny"]))
        candidate_rows.append(
            {
                "model": label,
                "window": label,
                "large_tail_day_count": len(large),
                "large_tail_rmse_cny": rmse(errors),
                "large_tail_mae_cny": statistics.fmean(abs(error) for error in errors),
            }
        )
    with (SOURCE_ROOT / "market_data" / "xop_prices.csv").open(encoding="utf-8-sig", newline="") as handle:
        moc_prices = {row["trade_day"]: float(row["close"]) for row in csv.DictReader(handle) if row.get("symbol") == "XOP"}
    moc_errors = []
    for row in large:
        p = predictions[str(row["redeem_day"])]
        estimate = SHARES * moc_prices[str(row["redeem_day"])] * float(p["fx_usd_cny"]) + float(p["pcf_estimate_cash_difference_cny"])
        moc_errors.append(estimate - float(p["actual_total_asset_cny"]))
    candidate_rows.append(
        {
            "model": "MOC_官方收盘价代理",
            "window": "日线官方收盘价",
            "large_tail_day_count": len(large),
            "large_tail_rmse_cny": rmse(moc_errors),
            "large_tail_mae_cny": statistics.fmean(abs(error) for error in moc_errors),
        }
    )
    candidate_rows.sort(key=lambda row: float(row["large_tail_rmse_cny"]))

    OUT.mkdir(parents=True, exist_ok=True)
    for path, rows in ((OUT / "large_tail_inverse_prices.csv", inverse_rows), (OUT / "large_tail_model_ranking.csv", candidate_rows)):
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

    best = candidate_rows[0]
    lines = [
        "# 尾盘剧烈变动日反算",
        "",
        f"筛选规则：`|15:59 收盘价 − 15:30 收盘价| ≥ {MOVE_THRESHOLD:.2f} USD`，共 {len(large)} 日。",
        "",
        "反算式：`隐含 XOP 价格 = (实际退款 + 实际现金差额 − PCF EstimateCashComponent) ÷ (996 × T日16:00汇率)`。",
        "",
        "| 日期 | 15:30 | 15:59 | 尾盘变动 | 反算隐含价 | 最近分钟 | 15:59 与隐含价差 |",
        "|---|---:|---:|---:|---:|---|---:|",
    ]
    lines.extend(
        f"| {row['redeem_day']} | {float(row['tail_start_1530_usd']):.2f} | {float(row['tail_last_1559_usd']):.2f} | {float(row['tail_change_usd']):+.2f} | {float(row['implied_xop_price_usd']):.4f} | {row['nearest_minute']} | {float(row['selected_1559_gap_usd']):+.4f} |"
        for row in large
    )
    lines.extend(
        [
            "",
            "## 剧烈尾盘日模型排名",
            "",
            "| 排名 | 模型 | RMSE | MAE |",
            "|---:|---|---:|---:|",
        ]
    )
    lines.extend(
        f"| {index} | {row['model']} | {float(row['large_tail_rmse_cny']):,.2f} | {float(row['large_tail_mae_cny']):,.2f} |"
        for index, row in enumerate(candidate_rows[:12], start=1)
    )
    lines.extend(
        [
            "",
            f"结论：剧烈尾盘日的最低误差模型为 **{best['model']}**。该压力测试仅有 {len(large)} 日，应与全样本结果共同使用。",
        ]
    )
    (OUT / "尾盘剧烈变动日反算.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Large tail days: {len(large)}; best: {best['model']} / RMSE {float(best['large_tail_rmse_cny']):,.2f} CNY")


if __name__ == "__main__":
    main()
