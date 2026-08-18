SELECT
    product,
    scenario,
    strategy_cagr,
    benchmark_cagr,
    annualized_excess_vs_benchmark,
    total_transaction_cost
FROM sensitivity
ORDER BY product, scenario;
