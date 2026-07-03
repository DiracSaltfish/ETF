from pathlib import Path
import json
from collections import defaultdict

from basket_loader import load_basket_document
from basket_models import ConnectionSettings
from ib_service import ib_connection, _snapshot, _load_portfolio_positions

config = json.loads(Path('/Users/ellis/Desktop/ETF交割/篮子管理/config.json').read_text(encoding='utf-8'))
basket = load_basket_document(config['basket_path'])
settings = ConnectionSettings(
    host=config['host'],
    port=int(config['port']),
    client_id=int(config['client_id']),
    account=str(config.get('account') or ''),
)

component_targets = {}
xop_target = 0
for row in basket.rows:
    if row.symbol.upper() == 'XOP':
        xop_target += row.signed_target
    else:
        component_targets[row.symbol.upper()] = component_targets.get(row.symbol.upper(), 0) + row.signed_target

with ib_connection(settings) as ib:
    snapshot = _snapshot(ib, settings)
    positions = _load_portfolio_positions(ib, snapshot.active_account)
    fills = ib.reqExecutions()

position_map = defaultdict(float)
for p in positions:
    position_map[p.symbol.upper()] += float(p.quantity)

execs_by_symbol = defaultdict(list)
agg_exec_qty = defaultdict(float)
agg_exec_value = defaultdict(float)
for fill in fills:
    symbol = fill.contract.symbol.upper()
    if symbol not in component_targets and symbol != 'XOP':
        continue
    side = str(fill.execution.side).upper()
    shares = float(fill.execution.shares)
    price = float(fill.execution.price)
    signed_qty = shares if side in {'BOT', 'BUY'} else -shares
    execs_by_symbol[symbol].append({
        'time': str(fill.execution.time),
        'side': side,
        'shares': shares,
        'price': price,
        'acct': str(fill.execution.acctNumber),
        'orderId': int(fill.execution.orderId or 0),
        'permId': int(fill.execution.permId or 0),
    })
    agg_exec_qty[symbol] += signed_qty
    agg_exec_value[symbol] += signed_qty * price

print('snapshot_time', snapshot.server_time)
print('account', snapshot.active_account)
print('basket_file', basket.path)
print('component_target_count', len(component_targets))
print('xop_target', xop_target)
print('--- COMPONENT CHECK ---')
matched = []
mismatched = []
for symbol in sorted(component_targets):
    target = component_targets[symbol]
    current = position_map.get(symbol, 0.0)
    delta = target - current
    row = {
        'symbol': symbol,
        'target': target,
        'current': current,
        'delta': delta,
        'exec_net': agg_exec_qty.get(symbol, 0.0),
        'exec_count': len(execs_by_symbol.get(symbol, [])),
    }
    if abs(delta) < 1e-9:
        matched.append(row)
    else:
        mismatched.append(row)

print('matched_count', len(matched))
print('mismatched_count', len(mismatched))
for row in mismatched:
    print('MISMATCH', row)

print('--- XOP CHECK ---')
print('xop_current_position', position_map.get('XOP', 0.0))
print('xop_exec_net', agg_exec_qty.get('XOP', 0.0))
print('xop_exec_count', len(execs_by_symbol.get('XOP', [])))
if execs_by_symbol.get('XOP'):
    for item in execs_by_symbol['XOP']:
        print('XOP_EXEC', item)

print('--- NUAI EXEC ---')
for item in execs_by_symbol.get('NUAI', []):
    print('NUAI_EXEC', item)

print('--- ALL MISMATCH EXEC DETAILS ---')
for row in mismatched:
    symbol = row['symbol']
    for item in execs_by_symbol.get(symbol, []):
        print(symbol, item)
