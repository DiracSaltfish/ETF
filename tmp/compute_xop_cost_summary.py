from basket_loader import load_basket_document
from basket_models import ConnectionSettings
from ib_service import ib_connection, _snapshot, _load_symbol_market_states
from pathlib import Path
import json

config = json.loads(Path('/Users/ellis/Desktop/ETF交割/篮子管理/config.json').read_text(encoding='utf-8'))
basket = load_basket_document(config['basket_path'])
settings = ConnectionSettings(config['host'], int(config['port']), int(config['client_id']), str(config.get('account') or ''))
rows = [r for r in basket.rows if r.quantity > 0]
component_rows = [r for r in rows if r.symbol.upper() != 'XOP']
xop_qty = next((r.quantity for r in rows if r.symbol.upper() == 'XOP'), 990)
symbols = tuple(sorted({r.symbol for r in component_rows} | {'XOP'}))

with ib_connection(settings) as ib:
    snap = _snapshot(ib, settings)
    states = {s.symbol: s for s in _load_symbol_market_states(ib, symbols)}


def mid(symbol):
    s = states[symbol]
    if s.bid and s.ask:
        return (s.bid + s.ask) / 2
    return s.market_price or s.last or s.close

def spread(symbol):
    s = states[symbol]
    if s.bid and s.ask:
        return max(s.ask - s.bid, 0.0)
    return 0.0

component_short_notional = sum(r.quantity * mid(r.symbol) for r in component_rows)
xop_notional = xop_qty * mid('XOP')
open_slippage_components = sum(r.quantity * spread(r.symbol) / 2 for r in component_rows)
open_slippage_xop = xop_qty * spread('XOP') / 2
open_slippage_total = open_slippage_components + open_slippage_xop
roundtrip_slippage_total = open_slippage_total * 2

# Fixed pricing
fixed_open = sum(max(1.0, 0.005 * r.quantity) for r in component_rows) + max(1.0, 0.005 * xop_qty)
fixed_roundtrip = fixed_open * 2

# Tiered baseline, <=300k monthly shares tier, excluding venue liquidity removing fees
# Includes IB base, clearing, CAT, SEC on sells, FINRA TAF on sells, pass-through fees.

def tier_base_for_order(qty):
    comm = max(0.35, 0.0035 * qty)
    clearing = 0.00020 * qty
    cat = 0.000003 * qty
    pass_through = comm * (0.000175 + 0.00056)
    return comm + clearing + cat + pass_through

open_tier_components = sum(tier_base_for_order(r.quantity) for r in component_rows)
open_tier_xop_buy = tier_base_for_order(xop_qty)
open_tier = open_tier_components + open_tier_xop_buy
# sell-side regs for opening component shorts
open_sec = 0.0000206 * component_short_notional
open_taf = 0.000195 * sum(r.quantity for r in component_rows)
open_tier += open_sec + open_taf

close_tier_components = sum(tier_base_for_order(r.quantity) for r in component_rows)
close_tier_xop_sell = tier_base_for_order(xop_qty)
close_tier = close_tier_components + close_tier_xop_sell
# sell-side regs for closing XOP long
close_sec = 0.0000206 * xop_notional
close_taf = 0.000195 * xop_qty
close_tier += close_sec + close_taf

tier_roundtrip = open_tier + close_tier

print('snapshot', snap.server_time)
print('component_short_notional', round(component_short_notional, 2))
print('xop_notional', round(xop_notional, 2))
print('gross_notional_open', round(component_short_notional + xop_notional, 2))
print('open_slippage_components', round(open_slippage_components, 2))
print('open_slippage_xop', round(open_slippage_xop, 2))
print('open_slippage_total', round(open_slippage_total, 2))
print('roundtrip_slippage_total', round(roundtrip_slippage_total, 2))
print('fixed_open', round(fixed_open, 2))
print('fixed_roundtrip', round(fixed_roundtrip, 2))
print('tier_open_baseline', round(open_tier, 2))
print('tier_roundtrip_baseline', round(tier_roundtrip, 2))

# output top contributors to spread/slippage
contribs = []
for r in component_rows:
    contribs.append((r.symbol, r.quantity * spread(r.symbol), r.quantity, spread(r.symbol), mid(r.symbol)))
contribs.append(('XOP', xop_qty * spread('XOP'), xop_qty, spread('XOP'), mid('XOP')))
contribs.sort(key=lambda x: x[1], reverse=True)
print('top_roundtrip_spread_cost_symbols')
for item in contribs[:12]:
    print(item)
