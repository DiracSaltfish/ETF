#!/usr/bin/env python3
"""Independent, reproducible calibration for 159518 redemption refunds.

Inputs below the sibling ``赎回收益计算器`` directory are opened read-only.  This
script deliberately writes only to its own analysis directory.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Callable


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT.parent / "赎回收益计算器"
DATA_DIR = ROOT / "data"
OUT_DIR = ROOT / "output"
PCF_DAYS = (
    date(2026, 6, 22),
    date(2026, 6, 23),
    date(2026, 6, 25),
    date(2026, 6, 29),
    date(2026, 6, 30),
)


@dataclass(frozen=True)
class Observation:
    redeem_day: date
    basket_count: int
    refund_cny: float
    cash_difference_cny: float
    manual_refund_used: bool
    basket_ids: str


@dataclass(frozen=True)
class PriceCandidate:
    name: str
    method: str
    window: str
    price_by_day: dict[date, float]


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_component_closes() -> dict[tuple[str, date], float]:
    with (DATA_DIR / "pcf_component_daily_closes.csv").open(encoding="utf-8-sig", newline="") as handle:
        return {
            (row["symbol"].strip(), date.fromisoformat(row["trade_day"])): float(row["close"])
            for row in csv.DictReader(handle)
        }


def read_fx() -> dict[str, dict[date, float]]:
    result: dict[str, dict[date, float]] = defaultdict(dict)
    with (SOURCE_ROOT / "fx_data" / "fx_rates.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("source") != "CFETS_REFERENCE_RATE" or row.get("pair") != "USD/CNY":
                continue
            time_label = (row.get("quote_time") or "").strip()
            if time_label not in {"CLOSE", "16:00"}:
                continue
            result[time_label][date.fromisoformat(row["trade_date"])] = float(row["rate"])
    return result


def next_fx_days(fx_by_day: dict[date, float], day: date, max_offset: int = 2) -> list[date]:
    dates = sorted(fx_by_day)
    try:
        start = dates.index(day)
    except ValueError as exc:
        raise KeyError(f"CFETS 缺少 {day} 的汇率") from exc
    selected = dates[start: start + max_offset + 1]
    if len(selected) != max_offset + 1:
        raise KeyError(f"CFETS 缺少 {day} 后的完整 T/T+1/T+2 汇率")
    return selected


def weighted_vwap(rows: list[dict[str, float]]) -> float:
    total_volume = sum(row["volume"] for row in rows)
    if total_volume <= 0:
        return statistics.fmean(row["close"] for row in rows)
    return sum(row["bar_vwap"] * row["volume"] for row in rows) / total_volume


def build_price_candidates() -> list[PriceCandidate]:
    sys.path.insert(0, str(SOURCE_ROOT))
    from market_data import CsvXopPriceProvider

    by_day: dict[date, list[dict[str, float]]] = defaultdict(list)
    with (DATA_DIR / "xop_tail_1m.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            by_day[date.fromisoformat(row["trade_day"])] .append(
                {
                    "minute": float(row["minute"].replace(":", ".")),
                    "minute_text": row["minute"],
                    "close": float(row["close"]),
                    "bar_vwap": float(row["bar_vwap"]),
                    "volume": float(row["volume"]),
                }
            )
    for rows in by_day.values():
        rows.sort(key=lambda row: row["minute_text"])
    if set(by_day) != set(PCF_DAYS):
        raise ValueError("尾盘分钟行情日期与待拟合赎回日期不一致")

    candidates: list[PriceCandidate] = []
    for index in range(30):
        minute = by_day[PCF_DAYS[0]][index]["minute_text"]
        candidates.append(
            PriceCandidate(
                name=f"点价_{minute}_1分钟收盘",
                method="point_close",
                window=minute,
                price_by_day={day: by_day[day][index]["close"] for day in PCF_DAYS},
            )
        )

    # MOC is modeled with the official RTH daily close recorded by TWS.
    # It is an official-close proxy, not evidence that a specific MOC order filled.
    daily_prices = CsvXopPriceProvider(SOURCE_ROOT / "market_data" / "xop_prices.csv")
    candidates.append(
        PriceCandidate(
            name="MOC_官方收盘价代理",
            method="moc_official_close",
            window="16:00 官方收盘价（TWS日线）",
            price_by_day={day: float(daily_prices.get_close(day)) for day in PCF_DAYS},
        )
    )

    boundary_indices = (0, 5, 10, 15, 20, 25, 30)
    for start_index in boundary_indices[:-1]:
        for end_index in boundary_indices:
            if end_index <= start_index:
                continue
            start = by_day[PCF_DAYS[0]][start_index]["minute_text"]
            end = "16:00" if end_index == 30 else by_day[PCF_DAYS[0]][end_index]["minute_text"]
            sample_rows = {day: by_day[day][start_index:end_index] for day in PCF_DAYS}
            label = f"{start}-{end}"
            candidates.extend(
                (
                    PriceCandidate(
                        name=f"TWAP_{label}",
                        method="twap",
                        window=label,
                        price_by_day={day: statistics.fmean(row["close"] for row in rows) for day, rows in sample_rows.items()},
                    ),
                    PriceCandidate(
                        name=f"VWAP_{label}",
                        method="vwap",
                        window=label,
                        price_by_day={day: weighted_vwap(rows) for day, rows in sample_rows.items()},
                    ),
                )
            )

    # Exhaust every 1-10 minute TWAP fully contained in 15:45-16:00 NY.
    # A one-minute TWAP equals that bar's close; timestamps are interval starts.
    for start_index in range(15, 30):
        for minutes in range(1, 11):
            end_index = start_index + minutes
            if end_index > 30:
                continue
            start = by_day[PCF_DAYS[0]][start_index]["minute_text"]
            end = "16:00" if end_index == 30 else by_day[PCF_DAYS[0]][end_index]["minute_text"]
            label = f"{start}-{end}（{minutes}分钟）"
            candidates.append(
                PriceCandidate(
                    name=f"滚动TWAP_{label}",
                    method="twap_1545_1600_rolling",
                    window=label,
                    price_by_day={
                        day: statistics.fmean(row["close"] for row in by_day[day][start_index:end_index])
                        for day in PCF_DAYS
                    },
                )
            )
    return candidates


def load_observations() -> list[Observation]:
    sys.path.insert(0, str(SOURCE_ROOT))
    import redemption_engine as engine

    config = json.loads((SOURCE_ROOT / "config.json").read_text(encoding="utf-8"))
    calculation = engine.calculate(
        {"QMT1": config["qmt1_path"], "QMT2": config["qmt2_path"]},
        config["ib_path"],
        Decimal(config["fx_rate"]),
        overrides=engine.load_overrides(SOURCE_ROOT / "ib_mapping_overrides.json"),
        market_holidays=[date.fromisoformat(item) for item in config.get("market_holidays", [])],
        transfer_contract_gap=int(config.get("transfer_contract_gap", 1000)),
        qmt_time_root=config.get("shared_folder_path"),
    )
    grouped: dict[date, list[object]] = defaultdict(list)
    for basket in calculation.baskets:
        if basket.refund_amount > 0 and basket.redeem_day in PCF_DAYS:
            grouped[basket.redeem_day].append(basket)
    result: list[Observation] = []
    for redeem_day, baskets in sorted(grouped.items()):
        refunds = [float(basket.refund_amount) / (basket.redeem_qty / 1_000_000) for basket in baskets]
        cash = [float(basket.cash_difference) / (basket.redeem_qty / 1_000_000) for basket in baskets]
        if max(refunds) - min(refunds) > 0.01:
            raise ValueError(f"{redeem_day} 同日篮子退款不一致，不能去重")
        result.append(
            Observation(
                redeem_day=redeem_day,
                basket_count=len(baskets),
                refund_cny=statistics.fmean(refunds),
                cash_difference_cny=statistics.fmean(cash),
                manual_refund_used=any(basket.manual_refund_applied for basket in baskets),
                basket_ids=", ".join(basket.id for basket in baskets),
            )
        )
    if {item.redeem_day for item in result} != set(PCF_DAYS):
        raise ValueError("交割单中可用退款观测与 PCF 日期不完整")
    return result


def component_equivalence() -> tuple[list[dict[str, object]], int]:
    sys.path.insert(0, str(SOURCE_ROOT))
    import szse_pcf
    from fx_rates import FxRateStore
    from market_data import CsvXopPriceProvider

    closes = read_component_closes()
    pcf_store = szse_pcf.SzsePcfStore(SOURCE_ROOT / "szse_pcf_cache")
    xop_prices = CsvXopPriceProvider(SOURCE_ROOT / "market_data" / "xop_prices.csv")
    fx_store = FxRateStore(SOURCE_ROOT / "fx_data" / "fx_rates.csv")
    rows: list[dict[str, object]] = []
    for pcf_day in PCF_DAYS:
        detail = pcf_store.ensure_fund_detail(pcf_day, "159518")
        components = [
            item for item in detail.components
            if item.get("UnderlyingSecurityIDSource") == "9999" and Decimal(item.get("ComponentShare", "0")) > 0
        ]
        missing = [item["UnderlyingSecurityID"] for item in components if (item["UnderlyingSecurityID"], pcf_day) not in closes]
        stock_value = sum(
            float(Decimal(component["ComponentShare"])) * closes[(component["UnderlyingSecurityID"], pcf_day)]
            for component in components
            if (component["UnderlyingSecurityID"], pcf_day) in closes
        )
        safe_fx = fx_store.get_usd_cny_safe_mid(pcf_day)
        cash_component_cny = float(Decimal(detail.metadata.get("CashComponent", "0")))
        cash_component_usd = cash_component_cny / float(safe_fx) if safe_fx else 0.0
        xop_close = float(xop_prices.get_close(pcf_day))
        rows.append(
            {
                "pcf_day": pcf_day.isoformat(),
                "pcf_trading_day": detail.metadata.get("TradingDay", ""),
                "pcf_pre_trading_day": detail.metadata.get("PreTradingDay", ""),
                "component_count": len(components),
                "component_share_sum": float(sum(Decimal(item["ComponentShare"]) for item in components)),
                "component_market_value_usd": stock_value,
                "cash_component_cny": cash_component_cny,
                "cash_component_usd": cash_component_usd,
                "xop_daily_close_usd": xop_close,
                "xop_equivalent_shares": (stock_value + cash_component_usd) / xop_close,
                "missing_component_count": len(missing),
                "missing_components": ",".join(missing),
            }
        )
    equivalent_values = [float(row["xop_equivalent_shares"]) for row in rows]
    shares = int(round(statistics.median(equivalent_values)))
    return rows, shares


def metrics(errors: list[float]) -> tuple[float, float, float]:
    return (
        math.sqrt(statistics.fmean(error * error for error in errors)),
        statistics.fmean(abs(error) for error in errors),
        max(abs(error) for error in errors),
    )


def fit_share_count(observations: list[Observation], factors: dict[date, float]) -> tuple[float, int]:
    numerator = sum(item.refund_cny * factors[item.redeem_day] for item in observations)
    denominator = sum(factors[item.redeem_day] ** 2 for item in observations)
    continuous = numerator / denominator
    return continuous, int(math.floor(continuous + 0.5))


def scores_for_share_count(
    observations: list[Observation], factors: dict[date, float], shares: int
) -> tuple[float, float, float, list[dict[str, object]]]:
    errors = [shares * factors[item.redeem_day] - item.refund_cny for item in observations]
    rows = [
        {
            "redeem_day": item.redeem_day.isoformat(),
            "actual_refund_cny": item.refund_cny,
            "prediction_cny": shares * factors[item.redeem_day],
            "prediction_minus_actual_cny": shares * factors[item.redeem_day] - item.refund_cny,
        }
        for item in observations
    ]
    return (*metrics(errors), rows)


def loo_rmse(observations: list[Observation], factors: dict[date, float]) -> float:
    errors: list[float] = []
    for held_out in observations:
        train = [item for item in observations if item.redeem_day != held_out.redeem_day]
        _, shares = fit_share_count(train, factors)
        errors.append(shares * factors[held_out.redeem_day] - held_out.refund_cny)
    return math.sqrt(statistics.fmean(error * error for error in errors))


def build_model_rows(
    observations: list[Observation], pcf_shares: int, price_candidates: list[PriceCandidate], fx_data: dict[str, dict[date, float]]
) -> list[dict[str, object]]:
    confirmed = observations
    qmt_auto = [item for item in observations if not item.manual_refund_used]
    rows: list[dict[str, object]] = []
    for fx_time, fx_by_day in fx_data.items():
        for fx_offset in range(3):
            fx_dates = {item.redeem_day: next_fx_days(fx_by_day, item.redeem_day)[fx_offset] for item in observations}
            for candidate in price_candidates:
                factors = {
                    item.redeem_day: candidate.price_by_day[item.redeem_day] * fx_by_day[fx_dates[item.redeem_day]]
                    for item in observations
                }
                fitted_continuous, fitted_integer = fit_share_count(confirmed, factors)
                confirmed_pcf = scores_for_share_count(confirmed, factors, pcf_shares)
                qmt_auto_pcf = scores_for_share_count(qmt_auto, factors, pcf_shares)
                confirmed_auto = scores_for_share_count(confirmed, factors, fitted_integer)
                rows.append(
                    {
                        "price_model": candidate.name,
                        "price_method": candidate.method,
                        "price_window_ny": candidate.window,
                        "fx_time": fx_time,
                        "fx_offset": f"T+{fx_offset}",
                        "fx_date_mapping": "; ".join(f"{day:%Y-%m-%d}->{fx_dates[day]:%Y-%m-%d}" for day in PCF_DAYS),
                        "pcf_constrained_shares": pcf_shares,
                        "confirmed_observation_count": len(confirmed),
                        "qmt_auto_observation_count": len(qmt_auto),
                        "pcf_confirmed_rmse_cny": confirmed_pcf[0],
                        "pcf_confirmed_mae_cny": confirmed_pcf[1],
                        "pcf_confirmed_max_abs_cny": confirmed_pcf[2],
                        "pcf_qmt_auto_rmse_cny": qmt_auto_pcf[0],
                        "pcf_qmt_auto_mae_cny": qmt_auto_pcf[1],
                        "unconstrained_shares_continuous": fitted_continuous,
                        "unconstrained_shares_integer": fitted_integer,
                        "auto_confirmed_rmse_cny": confirmed_auto[0],
                        "auto_confirmed_mae_cny": confirmed_auto[1],
                        "auto_confirmed_loo_rmse_cny": loo_rmse(confirmed, factors),
                    }
                )
    rows.sort(key=lambda row: (float(row["pcf_confirmed_rmse_cny"]), float(row["pcf_qmt_auto_rmse_cny"])))
    return rows


def detailed_predictions(
    model: dict[str, object], observations: list[Observation], candidates: list[PriceCandidate], fx_data: dict[str, dict[date, float]]) -> list[dict[str, object]]:
    candidate = next(item for item in candidates if item.name == model["price_model"])
    fx_by_day = fx_data[str(model["fx_time"])]
    offset = int(str(model["fx_offset"]).removeprefix("T+"))
    shares = int(model["pcf_constrained_shares"])
    rows = []
    for item in observations:
        fx_day = next_fx_days(fx_by_day, item.redeem_day)[offset]
        price = candidate.price_by_day[item.redeem_day]
        fx = fx_by_day[fx_day]
        prediction = shares * price * fx
        rows.append(
            {
                "redeem_day": item.redeem_day.isoformat(),
                "actual_refund_cny": item.refund_cny,
                "manual_refund_used": "yes" if item.manual_refund_used else "no",
                "same_day_basket_count": item.basket_count,
                "pcf_xop_shares": shares,
                "xop_price_usd": price,
                "fx_date": fx_day.isoformat(),
                "fx_time": model["fx_time"],
                "fx_usd_cny": fx,
                "predicted_refund_cny": prediction,
                "prediction_minus_actual_cny": prediction - item.refund_cny,
                "prediction_minus_actual_bp": (prediction / item.refund_cny - 1.0) * 10_000,
            }
        )
    return rows


def share_count_comparison(
    model: dict[str, object], observations: list[Observation], candidates: list[PriceCandidate], fx_data: dict[str, dict[date, float]]
) -> list[dict[str, object]]:
    candidate = next(item for item in candidates if item.name == model["price_model"])
    fx_by_day = fx_data[str(model["fx_time"])]
    offset = int(str(model["fx_offset"]).removeprefix("T+"))
    factors = {
        item.redeem_day: candidate.price_by_day[item.redeem_day] * fx_by_day[next_fx_days(fx_by_day, item.redeem_day)[offset]]
        for item in observations
    }
    confirmed = observations
    qmt_auto = [item for item in observations if not item.manual_refund_used]
    rows = []
    for shares in (990, 995, 996, 997, 998, 999, 1_000):
        confirmed_scores = scores_for_share_count(confirmed, factors, shares)
        qmt_auto_scores = scores_for_share_count(qmt_auto, factors, shares)
        rows.append(
            {
                "shares_per_1m_cu": shares,
                "price_model": model["price_model"],
                "fx_time": model["fx_time"],
                "fx_offset": model["fx_offset"],
                "confirmed_rmse_cny": confirmed_scores[0],
                "confirmed_mae_cny": confirmed_scores[1],
                "confirmed_max_abs_cny": confirmed_scores[2],
                "qmt_auto_rmse_cny": qmt_auto_scores[0],
                "qmt_auto_mae_cny": qmt_auto_scores[1],
            }
        )
    return rows


def report(
    component_rows: list[dict[str, object]], pcf_shares: int, observations: list[Observation], model_rows: list[dict[str, object]], predictions: list[dict[str, object]], share_rows: list[dict[str, object]], twap_primary_rows: list[dict[str, object]], moc_rows: list[dict[str, object]]
) -> str:
    best = model_rows[0]
    close_models = [row for row in model_rows if row["fx_time"] == "CLOSE"]
    best_close = close_models[0]
    near_best = [row for row in close_models if float(row["pcf_confirmed_rmse_cny"]) <= float(best_close["pcf_confirmed_rmse_cny"]) * 1.20][:10]
    component_values = [float(row["xop_equivalent_shares"]) for row in component_rows]
    qmt_auto_days = [item for item in observations if not item.manual_refund_used]
    lines = [
        "# 159518 赎回退款与 XOP 尾盘成交拟合",
        "",
        "## 结论（样本内回测，非执行保证）",
        "",
        f"- PCF 成分券按同日 TWS 收盘价逐券加总后，隐含 XOP 数量为 {min(component_values):.2f}–{max(component_values):.2f} 股，中位数取整为 **{pcf_shares} 股**。这是本分析的主篮子数量；990 股是现有程序的账务口径，并未在此被改动。",
        f"- 以 {pcf_shares} 股固定、使用 {len(observations)} 个已确认实际退款日（其中 2026-06-30 为你本次确认的外部实际退款）做选择时，CFETS `CLOSE` 口径的最佳尾盘模型为 **{best_close['price_model']} × {best_close['fx_offset']}**，RMSE 为 **{float(best_close['pcf_confirmed_rmse_cny']):,.2f} CNY**、MAE 为 **{float(best_close['pcf_confirmed_mae_cny']):,.2f} CNY**。",
        "- 将相近点价、VWAP 和 TWAP 一并比较后，证据仅能把等价估值区间收窄到美东收盘前约 5–10 分钟（点价以 15:54–15:59 最优）；不应把 15:56 解读为实际成交到秒。",
        f"- 若只看 QMT 自动匹配到的 {len(qmt_auto_days)} 个独立退款日，上述模型 RMSE 为 **{float(best_close['pcf_qmt_auto_rmse_cny']):,.2f} CNY**；该值保留作数据来源敏感性比较。",
        f"- 若允许同时自由拟合股数，最低样本内误差模型为 **{best['price_model']} / CFETS {best['fx_time']} / {best['fx_offset']}**，它反推 {float(best['unconstrained_shares_continuous']):.3f} 股（整数 {int(best['unconstrained_shares_integer'])}）。这与成交时段和汇率日期共线，不能据此单独认定确切成交分钟。",
        "",
        "## 2026-06-30 结算日核对",
        "",
        "- T+3 现金差额：**2026-07-03**。该路径只跳过周末，不使用美国休市日；交割单中的实际现金差额也是 2026-07-03 的 1,182.59 CNY。",
        "- T+6 现金替代退款：**2026-07-09**。计数为 7/1、7/2、跳过 7/3（美国休市）、7/6、7/7、7/8、7/9；因此相较未考虑 7/3 休市的情形延后一个工作日。",
        "- 本次拟合已将该篮子的 **1,041,618.00 CNY** 作为你确认的真实退款纳入正式样本；原程序内它以人工退款字段保存，是因为当前 QMT 文件无法自动把它与该篮子关联，并不表示本分析把它当作假设值。",
        "",
        "## PCF 成分券加总",
        "",
        "计算式：`Σ(PCF 成分券数量 × 同日 TWS 收盘价) + PCF CashComponent ÷ SAFE 中间价`，再除以 XOP 同日收盘价。CashComponent 的影响不足 0.4 股，但仍已纳入。",
        "",
        "| PCF 日 | 成分券数 | 成分券市值 (USD) | XOP 收盘 | 隐含 XOP 股数 |",
        "|---|---:|---:|---:|---:|",
    ]
    lines.extend(
        f"| {row['pcf_day']} | {int(row['component_count'])} | {float(row['component_market_value_usd']):,.2f} | {float(row['xop_daily_close_usd']):.2f} | {float(row['xop_equivalent_shares']):.3f} |"
        for row in component_rows
    )
    lines.extend(
        [
            "",
            "## 股数基准对照（保持最佳 CFETS CLOSE / 价格模型不变）",
            "",
            "| 每 100 万份对应 XOP | 已确认样本 RMSE | 已确认样本 MAE | QMT 自动样本 RMSE |",
            "|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {int(row['shares_per_1m_cu'])} | {float(row['confirmed_rmse_cny']):,.2f} | {float(row['confirmed_mae_cny']):,.2f} | {float(row['qmt_auto_rmse_cny']):,.2f} |"
        for row in share_rows
    )
    lines.extend(
        [
            "",
            "## CFETS CLOSE 口径下的近优模型",
            "",
            "| 排名 | 价格模型（美东） | 汇率日期 | 固定股数 | 已确认 RMSE | 已确认 MAE | QMT 自动 RMSE |",
            "|---:|---|---|---:|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {index} | {row['price_model']} | {row['fx_offset']} | {int(row['pcf_constrained_shares'])} | {float(row['pcf_confirmed_rmse_cny']):,.2f} | {float(row['pcf_confirmed_mae_cny']):,.2f} | {float(row['pcf_qmt_auto_rmse_cny']):,.2f} |"
        for index, row in enumerate(near_best, start=1)
    )
    lines.extend(
        [
            "",
            "## 15:45–16:00 滚动 TWAP（1–10 分钟）",
            "",
            "下表固定 997 股、CFETS `CLOSE`、T 日汇率；窗口采用 `[起始分钟, 结束分钟)`，例如 15:58–16:00 是两根 1 分钟 bar 的均价，1 分钟 TWAP 即单根 bar 收盘价。完整的所有汇率日期/时点敏感性见 `twap_1545_1600_all_scenarios.csv`。",
            "",
            "| 排名 | TWAP 窗口（美东） | 已确认 RMSE | 已确认 MAE | QMT 自动 RMSE |",
            "|---:|---|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {index} | {row['price_window_ny']} | {float(row['pcf_confirmed_rmse_cny']):,.2f} | {float(row['pcf_confirmed_mae_cny']):,.2f} | {float(row['pcf_qmt_auto_rmse_cny']):,.2f} |"
        for index, row in enumerate(twap_primary_rows[:15], start=1)
    )
    moc_primary = next(
        row for row in moc_rows
        if row["fx_time"] == "CLOSE" and row["fx_offset"] == "T+0"
    )
    moc_best = moc_rows[0]
    twap_1600_proxy = min(
        (
            row for row in model_rows
            if row["price_method"] == "twap_1545_1600_rolling"
            and row["fx_time"] == "16:00" and row["fx_offset"] == "T+0"
        ),
        key=lambda row: float(row["pcf_confirmed_rmse_cny"]),
    )
    lines.extend(
        [
            "",
            "## MOC（官方收盘价）情景",
            "",
            "MOC 价格使用本地 TWS 日线的 XOP RTH 官方收盘价代理。固定 997 股时，CFETS `CLOSE` / T 日的 MOC RMSE 为 "
            f"**{float(moc_primary['pcf_confirmed_rmse_cny']):,.2f} CNY**、MAE 为 **{float(moc_primary['pcf_confirmed_mae_cny']):,.2f} CNY**；"
            f"相比 15:54–15:57 三分钟 TWAP 的 {float(twap_primary_rows[0]['pcf_confirmed_rmse_cny']):,.2f} CNY RMSE 更高。",
            "",
            f"全汇率敏感性下，MOC 的最优组合是 **CFETS {moc_best['fx_time']} / {moc_best['fx_offset']}**，"
            f"RMSE 为 **{float(moc_best['pcf_confirmed_rmse_cny']):,.2f} CNY**。",
            "",
            "注意：日线收盘价只能模拟官方收盘价，不能证明实际存在或成交了一笔 MOC。对于 NYSE Arca ETP，若没有收盘拍卖，官方收盘价可采用交易所定义的替代计算，因此 MOC 是应保留的压力测试，而非当前首选拟合模型。",
            "",
            "## 当前样本下的最终建议",
            "",
            "- **主估值/拟合模型**：`退款 ≈ 997 × XOP 15:54–15:57（美东）三分钟 TWAP × T 日 CFETS USD/CNY`。在本地 `CLOSE` 口径下，RMSE 为 "
            f"**{float(twap_primary_rows[0]['pcf_confirmed_rmse_cny']):,.2f} CNY**。",
            "- **汇率日期**：采用 **T 日**。T+1、T+2 在相同 TWAP 窗口下均明显变差。汇率具体时点暂不能由 5 个样本稳健区分：本地 16:00 代理的最优结果为 "
            f"**{twap_1600_proxy['price_window_ny']} / {float(twap_1600_proxy['pcf_confirmed_rmse_cny']):,.2f} CNY RMSE**，"
            f"与 `CLOSE` 的 {float(twap_primary_rows[0]['pcf_confirmed_rmse_cny']):,.2f} CNY 差异很小；若未来可取得真正 16:30 CFETS 价，应直接替换并继续回测。",
            "- **对冲执行基准**：若目标是复制该估值，而非追求收盘拍卖流动性，可把 997 股等量拆在 15:54、15:55、15:56 三个一分钟切片（例如 332 / 332 / 333 股；方向按你的现有风险敞口相反设置）。",
            "- **MOC 的定位**：保留为收盘价压力测试或流动性优先时的备选，不作为主模型。它在 T 日 `CLOSE` 下的 RMSE 为 "
            f"**{float(moc_primary['pcf_confirmed_rmse_cny']):,.2f} CNY**，高于主 TWAP 模型。",
        ]
    )
    lines.extend(
        [
            "",
            "## 主模型逐日交叉验证",
            "",
            f"退款预测式：`{pcf_shares} × XOP 估值价格 × CFETS USD/CNY`。误差为预测值减交割单实际 ETF 申购退款，不含 ETF 现金差额。",
            "",
            "| 赎回日 | 实际退款 (CNY) | XOP 价格 (USD) | 汇率日/汇率 | 预测退款 (CNY) | 误差 (CNY) | 误差 (bp) |",
            "|---|---:|---:|---|---:|---:|---:|",
        ]
    )
    lines.extend(
        f"| {row['redeem_day']} | {float(row['actual_refund_cny']):,.2f} | {float(row['xop_price_usd']):.4f} | {row['fx_date']} / {float(row['fx_usd_cny']):.4f} | {float(row['predicted_refund_cny']):,.2f} | {float(row['prediction_minus_actual_cny']):,.2f} | {float(row['prediction_minus_actual_bp']):.2f} |"
        for row in predictions
    )
    lines.extend(
        [
            "",
            "## 解释与边界",
            "",
            "- TWS 数据为 1 分钟 bar，只能将成交识别到分钟/窗口，不能据此推断几分几秒或单笔订单。",
            "- 本模型把 PCF 成分券的现金替代款映射为等价 XOP 篮子价值；它不证明基金管理人实际在 XOP 上交易，更不能替代成分券逐笔成交回报。",
            "- `CLOSE` 在本地 CFETS 文件中当前等于该日最后可用小时价（6 月样本为 18:00）；同时已单列 16:00 口径作敏感性比较。文件没有 16:30 的逐笔价格，因此不能把 CLOSE 严格表述为 16:30 定盘。",
            "- T/T+1/T+2 按 CFETS 有效报价日顺延，而非自然日；完整排名见 `model_ranking.csv`。",
            "- 当前有 5 个独立的已确认退款日（6 月 23 日两篮子金额相同，按一个日样本处理）。样本仍偏少，模型可用于缩小对冲估值区间，但应继续累积后续赎回样本，再检验该窗口是否稳定。",
        ]
    )
    return "\n".join(lines) + "\n"


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    observations = load_observations()
    component_rows, pcf_shares = component_equivalence()
    price_candidates = build_price_candidates()
    fx_data = read_fx()
    model_rows = build_model_rows(observations, pcf_shares, price_candidates, fx_data)
    best_close = next(row for row in model_rows if row["fx_time"] == "CLOSE")
    predictions = detailed_predictions(best_close, observations, price_candidates, fx_data)
    share_rows = share_count_comparison(best_close, observations, price_candidates, fx_data)
    rolling_twap_rows = [
        row for row in model_rows
        if row["price_method"] == "twap_1545_1600_rolling"
    ]
    rolling_twap_primary = sorted(
        (
            row for row in rolling_twap_rows
            if row["fx_time"] == "CLOSE" and row["fx_offset"] == "T+0"
        ),
        key=lambda row: float(row["pcf_confirmed_rmse_cny"]),
    )
    moc_rows = [row for row in model_rows if row["price_method"] == "moc_official_close"]

    write_csv(
        OUT_DIR / "pcf_component_equivalence.csv",
        component_rows,
        list(component_rows[0]),
    )
    observation_rows = [
        {
            "redeem_day": item.redeem_day.isoformat(),
            "same_day_basket_count": item.basket_count,
            "actual_refund_cny_per_1m_cu": item.refund_cny,
            "actual_cash_difference_cny_per_1m_cu": item.cash_difference_cny,
            "manual_refund_used": "yes" if item.manual_refund_used else "no",
            "actual_refund_source": (
                "用户确认的外部实际退款（原程序以人工退款字段保存）"
                if item.manual_refund_used else "QMT交割单自动匹配"
            ),
            "basket_ids": item.basket_ids,
        }
        for item in observations
    ]
    write_csv(OUT_DIR / "settlement_observations.csv", observation_rows, list(observation_rows[0]))
    write_csv(OUT_DIR / "model_ranking.csv", model_rows, list(model_rows[0]))
    write_csv(OUT_DIR / "best_model_predictions.csv", predictions, list(predictions[0]))
    write_csv(OUT_DIR / "share_count_comparison.csv", share_rows, list(share_rows[0]))
    write_csv(
        OUT_DIR / "twap_1545_1600_all_scenarios.csv",
        rolling_twap_rows,
        list(rolling_twap_rows[0]),
    )
    write_csv(
        OUT_DIR / "twap_1545_1600_t0_close.csv",
        rolling_twap_primary,
        list(rolling_twap_primary[0]),
    )
    write_csv(OUT_DIR / "moc_scenario_comparison.csv", moc_rows, list(moc_rows[0]))
    (OUT_DIR / "拟合报告.md").write_text(
        report(component_rows, pcf_shares, observations, model_rows, predictions, share_rows, rolling_twap_primary, moc_rows),
        encoding="utf-8",
    )

    best = best_close
    print(
        f"PCF anchor: {pcf_shares} XOP shares; best constrained model: "
        f"{best['price_model']} / CFETS {best['fx_time']} {best['fx_offset']} / "
        f"confirmed-sample RMSE {float(best['pcf_confirmed_rmse_cny']):,.2f} CNY"
    )
    best_twap = rolling_twap_primary[0]
    print(
        f"Best 15:45-16:00 rolling TWAP: {best_twap['price_window_ny']} / "
        f"CFETS CLOSE T+0 / confirmed-sample RMSE {float(best_twap['pcf_confirmed_rmse_cny']):,.2f} CNY"
    )
    moc_primary = next(row for row in moc_rows if row["fx_time"] == "CLOSE" and row["fx_offset"] == "T+0")
    print(
        f"MOC official-close proxy: CFETS CLOSE T+0 / "
        f"confirmed-sample RMSE {float(moc_primary['pcf_confirmed_rmse_cny']):,.2f} CNY"
    )
    print(f"Outputs: {OUT_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
