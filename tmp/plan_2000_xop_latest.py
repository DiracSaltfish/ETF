from pathlib import Path
import json
from collections import defaultdict
import pandas as pd

from basket_models import ConnectionSettings
from ib_service import ib_connection, _snapshot, _load_portfolio_positions, _load_symbol_market_states

latest_path = Path('/Users/ellis/Desktop/ETF交割/tmp/xop_holdings_daily_latest.xlsx')
config = json.loads(Path('/Users/ellis/Desktop/ETF交割/篮子管理/config.json').read_text(encoding='utf-8'))
settings = ConnectionSettings(config['host'], int(config['port']), int(config['client_id']), str(config.get('account') or ''))

latest = pd.read_excel(latest_path, sheet_name='holdings', header=4)
latest = latest[pd.to_numeric(latest['Shares Held'], errors='coerce').notna()].copy()
latest['Ticker'] = latest['Ticker'].astype(str).str.upper().str.strip()
latest['Weight'] = pd.to_numeric(latest['Weight'], errors='coerce')
latest = latest[~latest['Ticker'].isin({'-', 'IXPU6'})].copy()
latest['trade_ticker'] = latest['Ticker'].replace({'2670549D': 'XOM'})

symbols = tuple(sorted(set(latest['trade_ticker'].tolist()) | {'XOP'}))

with ib_connection(settings) as ib:
    snapshot = _snapshot(ib, settings)
    positions = _load_portfolio_positions(ib, snapshot.active_account)
    market_states = {s.symbol: s for s in _load_symbol_market_states(ib, symbols)}

position_map = defaultdict(float)
for p in positions:
    position_map[p.symbol.upper()] += float(p.quantity)

def price_for(symbol: str) -> float:
    state = market_states.get(symbol)
    if state:
        if state.market_price:
            return float(state.market_price)
        if state.last:
            return float(state.last)
        if state.bid and state.ask:
            return float((state.bid + state.ask) / 2)
        if state.bid:
            return float(state.bid)
        if state.ask:
            return float(state.ask)
        if state.close:
            return float(state.close)
    return 0.0

xop_price = price_for('XOP')
target_xop_shares = 2000
nominal_usd = xop_price * target_xop_shares
weight_sum = latest['Weight'].sum()
latest['weight_renorm_pct'] = latest['Weight'] / weight_sum * 100.0
latest['market_price'] = latest['trade_ticker'].map(price_for)
latest['raw_target_shares_2000'] = nominal_usd * (latest['weight_renorm_pct'] / 100.0) / latest['market_price']
latest['target_short_shares_2000'] = latest['raw_target_shares_2000'].round().astype(int)
latest['current_position'] = latest['trade_ticker'].map(lambda s: position_map.get(s, 0.0))
latest['current_short_shares'] = latest['current_position'].map(lambda x: max(-x, 0.0))
latest['additional_sell_shares'] = (latest['target_short_shares_2000'] - latest['current_short_shares']).clip(lower=0).round().astype(int)
latest['cover_needed_shares'] = (latest['current_short_shares'] - latest['target_short_shares_2000']).clip(lower=0).round().astype(int)
latest['target_short_usd_2000'] = latest['target_short_shares_2000'] * latest['market_price']
latest['current_short_usd'] = latest['current_short_shares'] * latest['market_price']
latest['source_symbol_note'] = latest.apply(lambda r: 'official 2670549D mapped to tradable XOM' if r['Ticker'] == '2670549D' else '', axis=1)

sell_plan = latest[latest['additional_sell_shares'] > 0].copy().sort_values('additional_sell_shares', ascending=False)
cover_plan = latest[latest['cover_needed_shares'] > 0].copy().sort_values('cover_needed_shares', ascending=False)

print('snapshot_time', snapshot.server_time)
print('account', snapshot.active_account)
print('xop_price', xop_price)
print('target_xop_shares', target_xop_shares)
print('nominal_usd', round(nominal_usd, 2))
print('equity_row_count', len(latest))
print('weight_sum_raw', round(weight_sum, 6))
print('sell_plan_count', len(sell_plan))
print('cover_plan_count', len(cover_plan))
print('total_target_short_shares', int(latest['target_short_shares_2000'].sum()))
print('total_additional_sell_shares', int(sell_plan['additional_sell_shares'].sum()))
print('total_current_short_usd', round(latest['current_short_usd'].sum(), 2))
print('total_target_short_usd_2000', round(latest['target_short_usd_2000'].sum(), 2))
print('--- SELL PLAN ---')
for _, r in sell_plan.iterrows():
    print({
        'ticker': r['trade_ticker'],
        'official_ticker': r['Ticker'],
        'target_2000': int(r['target_short_shares_2000']),
        'current_short': int(round(r['current_short_shares'])),
        'add_sell': int(r['additional_sell_shares']),
        'weight_pct': round(r['weight_renorm_pct'], 6),
        'price': round(float(r['market_price']), 4),
        'note': r['source_symbol_note'],
    })
print('--- COVER PLAN ---')
for _, r in cover_plan.iterrows():
    print({
        'ticker': r['trade_ticker'],
        'official_ticker': r['Ticker'],
        'target_2000': int(r['target_short_shares_2000']),
        'current_short': int(round(r['current_short_shares'])),
        'cover_needed': int(r['cover_needed_shares']),
        'weight_pct': round(r['weight_renorm_pct'], 6),
        'price': round(float(r['market_price']), 4),
        'note': r['source_symbol_note'],
    })

out = latest[['Name','Ticker','trade_ticker','weight_renorm_pct','market_price','target_short_shares_2000','current_short_shares','additional_sell_shares','cover_needed_shares','source_symbol_note']].copy()
out_path = Path('/Users/ellis/Desktop/ETF交割/tmp/xop_2000_component_plan_latest.csv')
out.to_csv(out_path, index=False)
print('saved_csv', out_path)
