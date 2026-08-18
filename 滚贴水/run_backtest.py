from __future__ import annotations

import argparse
from pathlib import Path

from src.roll_discount_backtest import run_all


def main() -> None:
    parser = argparse.ArgumentParser(description="四类股指期货滚贴水回测")
    parser.add_argument("--config", default="config.yaml", help="配置文件路径")
    args = parser.parse_args()
    result = run_all(Path(args.config))
    columns = [
        "product",
        "start_date",
        "end_date",
        "strategy_cagr",
        "benchmark_cagr",
        "annualized_excess_vs_benchmark",
        "strategy_max_drawdown",
        "roll_count",
        "total_transaction_cost",
    ]
    print(result["summary"][columns].to_string(index=False))
    print(f"\n输出目录: {result['output_dir']}")


if __name__ == "__main__":
    main()
