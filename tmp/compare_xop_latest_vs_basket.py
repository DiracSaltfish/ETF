from pathlib import Path
import pandas as pd

basket_path = Path('/Users/ellis/Desktop/ETF交割/篮子管理/xop_990_share_short_basket_live_2026-07-02.xlsx')
latest_path = Path('/Users/ellis/Desktop/ETF交割/tmp/xop_holdings_daily_latest.xlsx')

basket_df = pd.read_excel(basket_path, sheet_name='Basket')
latest_df = pd.read_excel(latest_path, sheet_name='holdings', header=4)

latest_norm = pd.DataFrame({
    'ticker': latest_df['Ticker'].astype(str).str.upper().str.strip(),
    'latest_weight_pct': pd.to_numeric(latest_df['Weight'], errors='coerce'),
})
latest_norm = latest_norm.dropna(subset=['ticker', 'latest_weight_pct'])
# remove cash or non-equity if any
latest_norm = latest_norm[latest_norm['ticker'].str.fullmatch(r'[A-Z\.\-]+', na=False)]

basket_norm = basket_df[['ticker', 'weight_pct', 'target_short_shares', 'price']].copy()
basket_norm['ticker'] = basket_norm['ticker'].astype(str).str.upper().str.strip()
basket_norm['weight_pct'] = pd.to_numeric(basket_norm['weight_pct'], errors='coerce')

merged = latest_norm.merge(basket_norm, on='ticker', how='outer', indicator=True)
merged['weight_diff_bp'] = (merged['weight_pct'] - merged['latest_weight_pct']) * 100.0
only_latest = merged[merged['_merge'] == 'left_only'].copy()
only_basket = merged[merged['_merge'] == 'right_only'].copy()
in_both = merged[merged['_merge'] == 'both'].copy()

print('latest_count', len(latest_norm))
print('basket_count', len(basket_norm))
print('only_latest_count', len(only_latest))
print('only_basket_count', len(only_basket))
print('max_abs_weight_diff_bp', round(in_both['weight_diff_bp'].abs().max(), 6) if not in_both.empty else None)
print('sum_abs_weight_diff_bp', round(in_both['weight_diff_bp'].abs().sum(), 6) if not in_both.empty else None)
if len(only_latest):
    print('only_latest', only_latest[['ticker','latest_weight_pct']].to_dict('records'))
if len(only_basket):
    print('only_basket', only_basket[['ticker','weight_pct']].to_dict('records'))
print('top_weight_diffs', in_both[['ticker','latest_weight_pct','weight_pct','weight_diff_bp']].sort_values('weight_diff_bp', key=lambda s: s.abs(), ascending=False).head(10).to_dict('records'))
