SELECT
    product,
    product || ' / ' || index_name AS product_label,
    start_date,
    end_date,
    strategy_cagr,
    benchmark_cagr,
    annualized_excess_vs_benchmark,
    strategy_max_drawdown,
    tracking_error,
    roll_count,
    total_transaction_cost,
    median_basis_pct,
    discount_day_share
FROM summary;
