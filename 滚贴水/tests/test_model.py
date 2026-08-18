from __future__ import annotations

import pandas as pd
import pytest

from src.roll_discount_backtest import ProductSpec, run_product_backtest


def _config() -> dict:
    return {
        "annual_trading_days": 252,
        "notional_exposure": 1.0,
        "exchange_fee_rate": 0.0,
        "broker_fee_multiplier": 1.0,
        "slippage_points_per_side": 0.0,
        "cash_yield_annual": 0.0,
        "margin_rate": 0.12,
    }


def _spec() -> ProductSpec:
    return ProductSpec("IF", "IF", "CSI300", "000300", "H00300", 300, "unused")


def _benchmark(dates: pd.DatetimeIndex, values: list[float]) -> pd.DataFrame:
    return pd.DataFrame({"date": dates, "benchmark_close": values})


def test_same_contract_return_uses_futures_price_change() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    futures = pd.DataFrame(
        {"date": dates, "futures_close": [100.0, 102.0], "contract": ["IF2401", "IF2401"],
         "open_interest": [1, 1], "minute_volume": [1, 1]}
    )
    spot = pd.DataFrame({"date": dates, "spot_close": [100.0, 101.0]})
    benchmark = _benchmark(dates, [100.0, 101.5])
    daily, _, _ = run_product_backtest(futures, spot, benchmark, _spec(), _config())
    assert daily.loc[1, "futures_return"] == pytest.approx(0.02)
    assert daily.loc[1, "net_excess_return"] == pytest.approx(0.005)
    assert daily.loc[1, "spot_return"] == pytest.approx(0.01)
    assert daily.loc[1, "benchmark_return"] == pytest.approx(0.015)


def test_roll_day_removes_artificial_contract_jump() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    futures = pd.DataFrame(
        {"date": dates, "futures_close": [100.0, 90.0], "contract": ["IF2401", "IF2402"],
         "open_interest": [1, 1], "minute_volume": [1, 1]}
    )
    spot = pd.DataFrame({"date": dates, "spot_close": [100.0, 101.0]})
    benchmark = _benchmark(dates, [100.0, 102.0])
    daily, rolls, _ = run_product_backtest(futures, spot, benchmark, _spec(), _config())
    assert daily.loc[1, "futures_return"] == daily.loc[1, "spot_return"]
    assert rolls.loc[0, "quoted_contract_jump"] == pytest.approx(-0.1)


def test_round_trip_cost_has_open_and_close() -> None:
    dates = pd.to_datetime(["2024-01-02", "2024-01-03"])
    futures = pd.DataFrame(
        {"date": dates, "futures_close": [100.0, 100.0], "contract": ["IF2401", "IF2401"],
         "open_interest": [1, 1], "minute_volume": [1, 1]}
    )
    spot = pd.DataFrame({"date": dates, "spot_close": [100.0, 100.0]})
    cfg = _config()
    cfg["exchange_fee_rate"] = 0.001
    benchmark = _benchmark(dates, [100.0, 100.0])
    daily, _, _ = run_product_backtest(futures, spot, benchmark, _spec(), cfg)
    assert abs(daily["transaction_cost"].sum() - 0.002) < 1e-12
