from pathlib import Path
import json
from collections import defaultdict

from basket_loader import load_basket_document
from basket_models import ConnectionSettings
from ib_service import ib_connection, _snapshot, _load_portfolio_positions, _load_symbol_market_states

config = json.loads(Path('/Users/ellis/Desktop/ETF交割/篮子管理/config.json').read_text(encoding='utf-8'))
basket = load_basket_document(config['basket_path'])
settings = ConnectionSettings(
    host=config['host'],
    port=int(config['port']),
    client_id=int(config['client_id']),
    account=str(config.get('account') or ''),
)

basket_targets = {}
for row in basket.rows:
    basket_targets[row.symbol.upper()] = basket_targets.get(row.symbol.upper(), 0) + row.signed_target
component_targets = {k: v for k, v in basket_targets.items() if k != 'XOP'}
xop_target = basket_targets.get('XOP', 0)
all_symbols = tuple(sorted(set(component_targets) | {'XOP'}))

with ib_connection(settings) as ib:
    snapshot = _snapshot(ib, settings)
    positions = _load_portfolio_positions(ib, snapshot.active_account)
    market_states = {s.symbol: s for s in _load_symbol_market_states(ib, all_symbols)}
    summary_rows = ib.accountSummary(account=snapshot.active_account)

summary = {}
for row in summary_rows:
    if row.account != snapshot.active_account:
        continue
    if row.currency not in {'BASE', 'USD'}:
        continue
    summary.setdefault(row.tag, {})[row.currency] = row.value

position_map = defaultdict(float)
price_map = {}
market_value_map = defaultdict(float)
for p in positions:
    sym = p.symbol.upper()
    position_map[sym] += float(p.quantity)
    market_value_map[sym] += float(p.market_value)
    if p.market_price:
        price_map[sym] = float(p.market_price)

for sym, state in market_states.items():
    px = state.market_price or state.last or ((state.bid + state.ask) / 2 if state.bid and state.ask else 0.0) or state.close
    if px:
        price_map[sym] = px

def px(sym):
    return float(price_map.get(sym, 0.0))

# Current filled component shorts only: basket names with negative current position.
current_component_short_mv = 0.0
filled_component_rows = []
missing_rows = []
for sym, target in sorted(component_targets.items()):
    current = position_map.get(sym, 0.0)
    if current < 0:
        mv = abs(current) * px(sym)
        current_component_short_mv += mv
        filled_component_rows.append((sym, current, px(sym), mv, target))
    else:
        missing_rows.append((sym, target, current, px(sym)))

xop_990_mv = abs(xop_target) * px('XOP')
xop_current_net_mv = position_map.get('XOP', 0.0) * px('XOP')
component_vs_xop_990_diff = current_component_short_mv - xop_990_mv
component_vs_xop_990_diff_pct = component_vs_xop_990_diff / xop_990_mv if xop_990_mv else 0.0

# Naive Reg T estimates.
# A. "This trade package" lens: 50 basket shorts + 990 long XOP.
naive_trade_init = 0.5 * current_component_short_mv + 0.5 * xop_990_mv
naive_trade_maint_rough = 0.3 * current_component_short_mv + 0.25 * xop_990_mv

# B. Current account net position lens: component shorts + net long XOP.
naive_net_init = 0.5 * current_component_short_mv + 0.5 * max(xop_current_net_mv, 0.0)
naive_net_maint_rough = 0.3 * current_component_short_mv + 0.25 * max(xop_current_net_mv, 0.0)

def val(tag):
    data = summary.get(tag, {})
    raw = data.get('BASE') or data.get('USD')
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None

wanted_tags = [
    'NetLiquidation', 'EquityWithLoanValue', 'SMA', 'AvailableFunds', 'ExcessLiquidity',
    'InitMarginReq', 'MaintMarginReq', 'FullInitMarginReq', 'FullMaintMarginReq',
    'LookAheadInitMarginReq', 'LookAheadMaintMarginReq', 'LookAheadAvailableFunds',
    'LookAheadExcessLiquidity', 'BuyingPower', 'GrossPositionValue', 'RegTEquity', 'RegTMargin'
]

print('snapshot_time', snapshot.server_time)
print('account', snapshot.active_account)
print('xop_price', round(px('XOP'), 4))
print('xop_target_shares', xop_target)
print('xop_current_position', position_map.get('XOP', 0.0))
print('xop_990_market_value', round(xop_990_mv, 2))
print('xop_current_net_market_value', round(xop_current_net_mv, 2))
print('current_component_short_market_value', round(current_component_short_mv, 2))
print('component_vs_xop_990_diff', round(component_vs_xop_990_diff, 2))
print('component_vs_xop_990_diff_pct', round(component_vs_xop_990_diff_pct * 100, 4))
print('filled_component_count', len(filled_component_rows))
print('missing_component_count', len(missing_rows))
print('missing_components', missing_rows)
print('--- ACCOUNT SUMMARY ---')
for tag in wanted_tags:
    print(tag, val(tag))
print('--- NAIVE MARGIN ESTIMATE ---')
print('naive_trade_init_990xop_pair', round(naive_trade_init, 2))
print('naive_trade_maint_rough_990xop_pair', round(naive_trade_maint_rough, 2))
print('naive_net_init_current_account_positions', round(naive_net_init, 2))
print('naive_net_maint_rough_current_account_positions', round(naive_net_maint_rough, 2))
