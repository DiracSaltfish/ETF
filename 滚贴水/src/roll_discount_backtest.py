from __future__ import annotations

import json
import math
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml


@dataclass(frozen=True)
class ProductSpec:
    code: str
    name: str
    index_name: str
    index_symbol: str
    total_return_symbol: str
    multiplier: int
    futures_zip_glob: str


def load_config(path: str | Path) -> dict[str, Any]:
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def product_specs(config: dict[str, Any]) -> list[ProductSpec]:
    return [
        ProductSpec(code=code, **values)
        for code, values in config["products"].items()
    ]


def _last_bar_at_or_before_close(frame: pd.DataFrame, dt_col: str) -> pd.DataFrame:
    dt = pd.to_datetime(frame[dt_col], errors="coerce")
    valid = dt.notna() & (dt.dt.time >= pd.Timestamp("09:30").time()) & (
        dt.dt.time <= pd.Timestamp("15:00").time()
    )
    result = frame.loc[valid].copy()
    result["datetime"] = dt.loc[valid]
    result["date"] = result["datetime"].dt.normalize()
    return result.sort_values("datetime").groupby("date", as_index=False).tail(1)


def locate_futures_zip(data_root: Path, spec: ProductSpec) -> Path:
    matches = sorted(data_root.glob(spec.futures_zip_glob))
    if len(matches) != 1:
        raise FileNotFoundError(
            f"{spec.code}: expected one futures zip for {spec.futures_zip_glob}, found {matches}"
        )
    return matches[0]


def load_futures_daily(
    data_root: Path, spec: ProductSpec, end_date: str | pd.Timestamp
) -> tuple[pd.DataFrame, dict[str, Any]]:
    zip_path = locate_futures_zip(data_root, spec)
    daily_parts: list[pd.DataFrame] = []
    raw_rows = 0
    with zipfile.ZipFile(zip_path) as archive:
        csv_members = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(csv_members) != 1:
            raise ValueError(f"{zip_path} should contain exactly one CSV, found {csv_members}")
        with archive.open(csv_members[0]) as stream:
            for chunk in pd.read_csv(stream, chunksize=500_000, low_memory=False):
                chunk.columns = [str(column).lstrip("\ufeff") for column in chunk.columns]
                raw_rows += len(chunk)
                required = {"时间", "收盘价", "合约代码", "成交量", "持仓量"}
                missing = required.difference(chunk.columns)
                if missing:
                    raise ValueError(f"{spec.code} futures data missing columns: {sorted(missing)}")
                daily_parts.append(_last_bar_at_or_before_close(chunk, "时间"))

    combined = pd.concat(daily_parts, ignore_index=True)
    # 分块边界可能把同一天拆开，再聚合一次。
    combined = combined.sort_values("datetime").groupby("date", as_index=False).tail(1)
    combined = combined.loc[combined["date"] <= pd.Timestamp(end_date)].copy()
    combined["futures_close"] = pd.to_numeric(combined["收盘价"], errors="coerce")
    combined["contract"] = combined["合约代码"].astype(str).str.strip().str.upper()
    combined["open_interest"] = pd.to_numeric(combined["持仓量"], errors="coerce")
    combined["minute_volume"] = pd.to_numeric(combined["成交量"], errors="coerce")
    result = combined[["date", "futures_close", "contract", "open_interest", "minute_volume"]]
    result = result.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)

    quality = {
        "product": spec.code,
        "futures_zip": str(zip_path),
        "raw_minute_rows": int(raw_rows),
        "daily_rows": int(len(result)),
        "start_date": result["date"].min().date().isoformat(),
        "end_date": result["date"].max().date().isoformat(),
        "null_close_rows": int(result["futures_close"].isna().sum()),
        "nonpositive_close_rows": int((result["futures_close"] <= 0).sum()),
        "contract_count": int(result["contract"].nunique()),
        "contract_switches": int(result["contract"].ne(result["contract"].shift()).sum() - 1),
    }
    return result, quality


def load_index_daily(
    data_root: Path,
    spec: ProductSpec,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    index_dir = data_root / "A股分钟数据" / "A股_分时数据_指数" / "1分钟_按年汇总"
    parts: list[pd.DataFrame] = []
    source_files: list[str] = []
    missing_years: list[int] = []
    raw_rows = 0
    for year in range(start_date.year, end_date.year + 1):
        zip_path = index_dir / f"{year}_1min.zip"
        if not zip_path.exists():
            missing_years.append(year)
            continue
        member = f"{spec.index_symbol}_{year}.csv"
        with zipfile.ZipFile(zip_path) as archive:
            if member not in archive.namelist():
                missing_years.append(year)
                continue
            with archive.open(member) as stream:
                frame = pd.read_csv(stream, low_memory=False)
        frame.columns = [str(column).lstrip("\ufeff") for column in frame.columns]
        required = {"时间", "收盘价"}
        missing = required.difference(frame.columns)
        if missing:
            raise ValueError(f"{member} missing columns: {sorted(missing)}")
        raw_rows += len(frame)
        daily = _last_bar_at_or_before_close(frame, "时间")
        daily["spot_close"] = pd.to_numeric(daily["收盘价"], errors="coerce")
        parts.append(daily[["date", "spot_close"]])
        source_files.append(str(zip_path) + "::" + member)

    if not parts:
        raise FileNotFoundError(f"No index data found for {spec.code}/{spec.index_symbol}")
    result = pd.concat(parts, ignore_index=True).sort_values("date")
    result = result.drop_duplicates("date", keep="last")
    result = result.loc[(result["date"] >= start_date) & (result["date"] <= end_date)]
    result = result.reset_index(drop=True)
    quality = {
        "product": spec.code,
        "index_symbol": spec.index_symbol,
        "index_name": spec.index_name,
        "index_source_files": source_files,
        "missing_years": missing_years,
        "raw_minute_rows": int(raw_rows),
        "daily_rows": int(len(result)),
        "start_date": result["date"].min().date().isoformat(),
        "end_date": result["date"].max().date().isoformat(),
        "null_close_rows": int(result["spot_close"].isna().sum()),
        "nonpositive_close_rows": int((result["spot_close"] <= 0).sum()),
    }
    return result, quality


def load_total_return_daily(
    benchmark_data_dir: Path,
    spec: ProductSpec,
    start_date: pd.Timestamp,
    end_date: pd.Timestamp,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    csv_path = benchmark_data_dir / f"{spec.total_return_symbol}.csv"
    if not csv_path.exists():
        raise FileNotFoundError(
            f"Missing total-return benchmark {csv_path}; run fetch_total_return_indices.py first"
        )
    frame = pd.read_csv(csv_path)
    required = {"date", "close", "index_code", "index_name"}
    missing = required.difference(frame.columns)
    if missing:
        raise ValueError(f"{csv_path} missing columns: {sorted(missing)}")
    frame["date"] = pd.to_datetime(frame["date"], errors="coerce").dt.normalize()
    frame["benchmark_close"] = pd.to_numeric(frame["close"], errors="coerce")
    code_values = frame["index_code"].dropna().astype(str).unique().tolist()
    if code_values != [spec.total_return_symbol]:
        raise ValueError(
            f"{csv_path} index_code must be {spec.total_return_symbol}, found {code_values}"
        )
    result = frame.loc[
        (frame["date"] >= start_date) & (frame["date"] <= end_date),
        ["date", "benchmark_close"],
    ]
    result = result.sort_values("date").drop_duplicates("date", keep="last").reset_index(drop=True)
    if result.empty:
        raise ValueError(f"No total-return data for {spec.code} in requested date range")
    quality = {
        "product": spec.code,
        "benchmark_type": "gross_total_return_index",
        "benchmark_symbol": spec.total_return_symbol,
        "benchmark_name": str(frame["index_name"].dropna().iloc[0]),
        "benchmark_source_file": str(csv_path),
        "official_source": "https://www.csindex.com.cn/csindex-home/perf/index-perf",
        "daily_rows": int(len(result)),
        "start_date": result["date"].min().date().isoformat(),
        "end_date": result["date"].max().date().isoformat(),
        "null_close_rows": int(result["benchmark_close"].isna().sum()),
        "nonpositive_close_rows": int((result["benchmark_close"] <= 0).sum()),
    }
    return result, quality


def _drawdown(wealth: pd.Series) -> pd.Series:
    return wealth / wealth.cummax() - 1.0


def _annualized_return(wealth: pd.Series, dates: pd.Series, trading_days: int) -> float:
    observations = max(int(wealth.notna().sum()) - 1, 1)
    return float(wealth.iloc[-1] ** (trading_days / observations) - 1.0)


def _annualized_volatility(returns: pd.Series, trading_days: int) -> float:
    return float(returns.std(ddof=1) * math.sqrt(trading_days))


def build_roll_events(daily: pd.DataFrame) -> pd.DataFrame:
    switch = daily["is_roll"].fillna(False)
    events = daily.loc[switch].copy()
    if events.empty:
        return pd.DataFrame()
    events["old_contract"] = daily["contract"].shift().loc[events.index]
    events["new_contract"] = events["contract"]
    events["previous_futures_close"] = daily["futures_close"].shift().loc[events.index]
    events["quoted_contract_jump"] = (
        events["futures_close"] / events["previous_futures_close"] - 1.0
    )
    events["jump_minus_spot_return"] = events["quoted_contract_jump"] - events["spot_return"]
    return events[
        [
            "date",
            "old_contract",
            "new_contract",
            "previous_futures_close",
            "futures_close",
            "spot_return",
            "quoted_contract_jump",
            "jump_minus_spot_return",
            "transaction_cost",
        ]
    ].reset_index(drop=True)


def run_product_backtest(
    futures: pd.DataFrame,
    spot: pd.DataFrame,
    benchmark: pd.DataFrame,
    spec: ProductSpec,
    config: dict[str, Any],
) -> tuple[pd.DataFrame, pd.DataFrame, dict[str, Any]]:
    daily = futures.merge(spot, on="date", how="inner", validate="one_to_one")
    daily = daily.merge(benchmark, on="date", how="inner", validate="one_to_one")
    daily = daily.sort_values("date").reset_index(drop=True)
    daily = daily.dropna(
        subset=["futures_close", "spot_close", "benchmark_close", "contract"]
    )
    daily = daily.loc[
        (daily["futures_close"] > 0)
        & (daily["spot_close"] > 0)
        & (daily["benchmark_close"] > 0)
    ].copy()
    if len(daily) < 2:
        raise ValueError(f"Insufficient aligned data for {spec.code}")

    annual_days = int(config["annual_trading_days"])
    exposure = float(config["notional_exposure"])
    fee_rate = float(config["exchange_fee_rate"]) * float(config["broker_fee_multiplier"])
    slippage = float(config["slippage_points_per_side"])
    cash_yield = float(config["cash_yield_annual"])
    margin_rate = float(config["margin_rate"])

    daily["spot_return"] = daily["spot_close"].pct_change()
    daily["benchmark_return"] = daily["benchmark_close"].pct_change()
    daily["raw_futures_close_return"] = daily["futures_close"].pct_change()
    daily["is_roll"] = daily["contract"].ne(daily["contract"].shift())
    daily.loc[daily.index[0], "is_roll"] = False

    # 主连换合约日不存在新旧合约同刻价格。用现货日收益替代该日的主连跳变，
    # 防止把合约价差直接当成当日损益；基差收敛从随后同合约日的真实价格变化体现。
    daily["futures_return"] = daily["raw_futures_close_return"].where(
        ~daily["is_roll"], daily["spot_return"]
    )
    daily.loc[
        daily.index[0],
        ["spot_return", "benchmark_return", "raw_futures_close_return", "futures_return"],
    ] = 0.0

    one_side_cost = fee_rate * exposure + slippage / daily["futures_close"] * exposure
    daily["transaction_cost"] = 0.0
    daily.loc[daily["is_roll"], "transaction_cost"] = 2.0 * one_side_cost.loc[daily["is_roll"]]
    daily.loc[daily.index[0], "transaction_cost"] += one_side_cost.iloc[0]
    daily.loc[daily.index[-1], "transaction_cost"] += one_side_cost.iloc[-1]

    cash_fraction = max(0.0, 1.0 - margin_rate * exposure)
    daily_cash_return = cash_fraction * ((1.0 + cash_yield) ** (1.0 / annual_days) - 1.0)
    daily["cash_return"] = daily_cash_return
    daily["gross_strategy_return"] = exposure * daily["futures_return"] + daily["cash_return"]
    daily["net_strategy_return"] = daily["gross_strategy_return"] - daily["transaction_cost"]
    daily["gross_excess_return"] = daily["gross_strategy_return"] - daily["benchmark_return"]
    daily["net_excess_return"] = daily["net_strategy_return"] - daily["benchmark_return"]
    daily["strategy_wealth"] = (1.0 + daily["net_strategy_return"]).cumprod()
    daily["gross_strategy_wealth"] = (1.0 + daily["gross_strategy_return"]).cumprod()
    daily["spot_wealth"] = (1.0 + daily["spot_return"]).cumprod()
    daily["benchmark_wealth"] = (1.0 + daily["benchmark_return"]).cumprod()
    daily["relative_wealth"] = daily["strategy_wealth"] / daily["benchmark_wealth"]
    daily["relative_wealth_vs_price_index"] = daily["strategy_wealth"] / daily["spot_wealth"]
    daily["strategy_drawdown"] = _drawdown(daily["strategy_wealth"])
    daily["spot_drawdown"] = _drawdown(daily["spot_wealth"])
    daily["benchmark_drawdown"] = _drawdown(daily["benchmark_wealth"])
    daily["basis_points"] = daily["futures_close"] - daily["spot_close"]
    daily["basis_pct"] = daily["futures_close"] / daily["spot_close"] - 1.0
    daily["product"] = spec.code

    rolls = build_roll_events(daily)
    strategy_cagr = _annualized_return(daily["strategy_wealth"], daily["date"], annual_days)
    gross_cagr = _annualized_return(daily["gross_strategy_wealth"], daily["date"], annual_days)
    spot_cagr = _annualized_return(daily["spot_wealth"], daily["date"], annual_days)
    benchmark_cagr = _annualized_return(daily["benchmark_wealth"], daily["date"], annual_days)
    excess_ann = strategy_cagr - benchmark_cagr
    tracking = daily["net_strategy_return"] - daily["benchmark_return"]
    summary = {
        "product": spec.code,
        "product_name": spec.name,
        "index_name": spec.index_name,
        "benchmark_name": f"{spec.index_name}全收益指数",
        "benchmark_symbol": spec.total_return_symbol,
        "start_date": daily["date"].min().date().isoformat(),
        "end_date": daily["date"].max().date().isoformat(),
        "trading_days": int(len(daily)),
        "roll_count": int(daily["is_roll"].sum()),
        "strategy_total_return": float(daily["strategy_wealth"].iloc[-1] - 1.0),
        "gross_strategy_total_return": float(daily["gross_strategy_wealth"].iloc[-1] - 1.0),
        "spot_total_return": float(daily["spot_wealth"].iloc[-1] - 1.0),
        "benchmark_total_return": float(daily["benchmark_wealth"].iloc[-1] - 1.0),
        "strategy_cagr": strategy_cagr,
        "gross_strategy_cagr": gross_cagr,
        "spot_cagr": spot_cagr,
        "benchmark_cagr": benchmark_cagr,
        "annualized_excess_vs_benchmark": excess_ann,
        "strategy_annualized_volatility": _annualized_volatility(daily["net_strategy_return"], annual_days),
        "tracking_error": _annualized_volatility(tracking, annual_days),
        "information_ratio": float(tracking.mean() / tracking.std(ddof=1) * math.sqrt(annual_days))
        if tracking.std(ddof=1) > 0
        else np.nan,
        "strategy_max_drawdown": float(daily["strategy_drawdown"].min()),
        "spot_max_drawdown": float(daily["spot_drawdown"].min()),
        "benchmark_max_drawdown": float(daily["benchmark_drawdown"].min()),
        "total_transaction_cost": float(daily["transaction_cost"].sum()),
        "median_basis_pct": float(daily["basis_pct"].median()),
        "discount_day_share": float((daily["basis_pct"] < 0).mean()),
        "aligned_days_share_of_futures": float(len(daily) / len(futures)),
    }
    return daily, rolls, summary


def annual_summary(daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for (product, year), group in daily.assign(year=daily["date"].dt.year).groupby(["product", "year"]):
        strategy = float((1.0 + group["net_strategy_return"]).prod() - 1.0)
        spot = float((1.0 + group["spot_return"]).prod() - 1.0)
        benchmark = float((1.0 + group["benchmark_return"]).prod() - 1.0)
        rows.append(
            {
                "product": product,
                "year": int(year),
                "strategy_return": strategy,
                "spot_return": spot,
                "benchmark_return": benchmark,
                "excess_return": strategy - benchmark,
                "roll_count": int(group["is_roll"].sum()),
                "transaction_cost": float(group["transaction_cost"].sum()),
                "median_basis_pct": float(group["basis_pct"].median()),
                "discount_day_share": float((group["basis_pct"] < 0).mean()),
            }
        )
    return pd.DataFrame(rows).sort_values(["product", "year"]).reset_index(drop=True)


def build_equal_weight_portfolio(
    daily: pd.DataFrame, annual_days: int
) -> tuple[pd.DataFrame, dict[str, Any]]:
    fields = [
        "net_strategy_return",
        "gross_strategy_return",
        "spot_return",
        "benchmark_return",
        "transaction_cost",
    ]
    common = daily.pivot(index="date", columns="product", values=fields).dropna()
    portfolio = pd.DataFrame(index=common.index)
    for field in fields:
        portfolio[field] = common[field].mean(axis=1)
    portfolio = portfolio.reset_index()
    portfolio["strategy_wealth"] = (1.0 + portfolio["net_strategy_return"]).cumprod()
    portfolio["gross_strategy_wealth"] = (1.0 + portfolio["gross_strategy_return"]).cumprod()
    portfolio["spot_wealth"] = (1.0 + portfolio["spot_return"]).cumprod()
    portfolio["benchmark_wealth"] = (1.0 + portfolio["benchmark_return"]).cumprod()
    portfolio["relative_wealth"] = portfolio["strategy_wealth"] / portfolio["benchmark_wealth"]
    portfolio["relative_wealth_vs_price_index"] = (
        portfolio["strategy_wealth"] / portfolio["spot_wealth"]
    )
    portfolio["strategy_drawdown"] = _drawdown(portfolio["strategy_wealth"])
    portfolio["spot_drawdown"] = _drawdown(portfolio["spot_wealth"])
    portfolio["benchmark_drawdown"] = _drawdown(portfolio["benchmark_wealth"])
    strategy_cagr = _annualized_return(portfolio["strategy_wealth"], portfolio["date"], annual_days)
    gross_cagr = _annualized_return(portfolio["gross_strategy_wealth"], portfolio["date"], annual_days)
    spot_cagr = _annualized_return(portfolio["spot_wealth"], portfolio["date"], annual_days)
    benchmark_cagr = _annualized_return(
        portfolio["benchmark_wealth"], portfolio["date"], annual_days
    )
    tracking = portfolio["net_strategy_return"] - portfolio["benchmark_return"]
    summary = {
        "portfolio": "IF/IH/IC/IM等权",
        "start_date": portfolio["date"].min().date().isoformat(),
        "end_date": portfolio["date"].max().date().isoformat(),
        "trading_days": int(len(portfolio)),
        "strategy_total_return": float(portfolio["strategy_wealth"].iloc[-1] - 1.0),
        "gross_strategy_total_return": float(portfolio["gross_strategy_wealth"].iloc[-1] - 1.0),
        "spot_total_return": float(portfolio["spot_wealth"].iloc[-1] - 1.0),
        "benchmark_total_return": float(portfolio["benchmark_wealth"].iloc[-1] - 1.0),
        "strategy_cagr": strategy_cagr,
        "gross_strategy_cagr": gross_cagr,
        "spot_cagr": spot_cagr,
        "benchmark_cagr": benchmark_cagr,
        "annualized_excess_vs_benchmark": strategy_cagr - benchmark_cagr,
        "strategy_annualized_volatility": _annualized_volatility(portfolio["net_strategy_return"], annual_days),
        "tracking_error": _annualized_volatility(tracking, annual_days),
        "information_ratio": float(tracking.mean() / tracking.std(ddof=1) * math.sqrt(annual_days))
        if tracking.std(ddof=1) > 0
        else np.nan,
        "strategy_max_drawdown": float(portfolio["strategy_drawdown"].min()),
        "spot_max_drawdown": float(portfolio["spot_drawdown"].min()),
        "benchmark_max_drawdown": float(portfolio["benchmark_drawdown"].min()),
        "total_transaction_cost": float(portfolio["transaction_cost"].sum()),
    }
    return portfolio, summary


def build_sensitivity(daily: pd.DataFrame, config: dict[str, Any]) -> pd.DataFrame:
    scenarios = [
        ("zero_cost", 0.0, 0.0, 0.0),
        (
            "base",
            float(config["exchange_fee_rate"]) * float(config["broker_fee_multiplier"]),
            float(config["slippage_points_per_side"]),
            float(config["cash_yield_annual"]),
        ),
        ("conservative_cost", float(config["exchange_fee_rate"]) * 2.0, 0.4, 0.0),
        (
            "base_cost_plus_2pct_cash",
            float(config["exchange_fee_rate"]) * float(config["broker_fee_multiplier"]),
            float(config["slippage_points_per_side"]),
            0.02,
        ),
    ]
    annual_days = int(config["annual_trading_days"])
    exposure = float(config["notional_exposure"])
    cash_fraction = max(0.0, 1.0 - float(config["margin_rate"]) * exposure)
    rows: list[dict[str, Any]] = []
    for product, group in daily.groupby("product", sort=False):
        group = group.sort_values("date").reset_index(drop=True)
        sides = group["is_roll"].astype(float) * 2.0
        sides.iloc[0] += 1.0
        sides.iloc[-1] += 1.0
        benchmark_wealth = (1.0 + group["benchmark_return"]).cumprod()
        benchmark_cagr = _annualized_return(benchmark_wealth, group["date"], annual_days)
        for scenario, fee_rate, slip_points, cash_yield in scenarios:
            transaction_cost = sides * (
                fee_rate * exposure + slip_points / group["futures_close"] * exposure
            )
            cash_return = cash_fraction * ((1.0 + cash_yield) ** (1.0 / annual_days) - 1.0)
            returns = exposure * group["futures_return"] + cash_return - transaction_cost
            wealth = (1.0 + returns).cumprod()
            strategy_cagr = _annualized_return(wealth, group["date"], annual_days)
            rows.append(
                {
                    "product": product,
                    "scenario": scenario,
                    "strategy_cagr": strategy_cagr,
                    "benchmark_cagr": benchmark_cagr,
                    "annualized_excess_vs_benchmark": strategy_cagr - benchmark_cagr,
                    "total_transaction_cost": float(transaction_cost.sum()),
                }
            )
    return pd.DataFrame(rows)


def run_all(config_path: str | Path) -> dict[str, Any]:
    config_path = Path(config_path).resolve()
    config = load_config(config_path)
    data_root = Path(config["data_root"])
    output_dir = (config_path.parent / config["output_dir"]).resolve()
    benchmark_data_dir = (config_path.parent / config["benchmark_data_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    all_daily: list[pd.DataFrame] = []
    all_rolls: list[pd.DataFrame] = []
    summaries: list[dict[str, Any]] = []
    quality_records: list[dict[str, Any]] = []

    for spec in product_specs(config):
        futures, futures_quality = load_futures_daily(data_root, spec, config["end_date"])
        spot, spot_quality = load_index_daily(
            data_root,
            spec,
            futures["date"].min(),
            min(futures["date"].max(), pd.Timestamp(config["end_date"])),
        )
        benchmark, benchmark_quality = load_total_return_daily(
            benchmark_data_dir,
            spec,
            futures["date"].min(),
            min(futures["date"].max(), pd.Timestamp(config["end_date"])),
        )
        daily, rolls, summary = run_product_backtest(
            futures, spot, benchmark, spec, config
        )
        all_daily.append(daily)
        if not rolls.empty:
            rolls.insert(0, "product", spec.code)
            all_rolls.append(rolls)
        summaries.append(summary)
        quality_records.extend(
            [
                {"source_type": "futures", **futures_quality},
                {"source_type": "spot_index", **spot_quality},
                {"source_type": "total_return_benchmark", **benchmark_quality},
            ]
        )

    daily_all = pd.concat(all_daily, ignore_index=True)
    rolls_all = pd.concat(all_rolls, ignore_index=True) if all_rolls else pd.DataFrame()
    summary_frame = pd.DataFrame(summaries)
    annual_frame = annual_summary(daily_all)
    sensitivity_frame = build_sensitivity(daily_all, config)

    daily_all.to_csv(output_dir / "daily_returns.csv", index=False, encoding="utf-8-sig")
    rolls_all.to_csv(output_dir / "roll_events.csv", index=False, encoding="utf-8-sig")
    summary_frame.to_csv(output_dir / "summary.csv", index=False, encoding="utf-8-sig")
    annual_frame.to_csv(output_dir / "annual_returns.csv", index=False, encoding="utf-8-sig")
    sensitivity_frame.to_csv(output_dir / "sensitivity.csv", index=False, encoding="utf-8-sig")
    with (output_dir / "data_quality.json").open("w", encoding="utf-8") as handle:
        json.dump(quality_records, handle, ensure_ascii=False, indent=2)
    with (output_dir / "run_metadata.json").open("w", encoding="utf-8") as handle:
        json.dump(
            {
                "config_path": str(config_path),
                "data_root": str(data_root),
                "model_version": "1.2",
                "roll_day_method": "replace main-series contract-switch jump with same-day spot return",
                "benchmark_method": "official gross total-return index with cash dividends reinvested",
                "portfolio_method": "four products tested independently; no equal-weight portfolio",
            },
            handle,
            ensure_ascii=False,
            indent=2,
        )
    return {
        "daily": daily_all,
        "rolls": rolls_all,
        "summary": summary_frame,
        "annual": annual_frame,
        "sensitivity": sensitivity_frame,
        "quality": quality_records,
        "output_dir": output_dir,
    }
