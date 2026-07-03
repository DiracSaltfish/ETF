from basket_loader import load_basket_document
from basket_models import ConnectionSettings
from ib_service import ib_connection, _snapshot, _load_symbol_market_states
from pathlib import Path
import json

config_path = Path('/Users/ellis/Desktop/ETF交割/篮子管理/config.json')
config = json.loads(config_path.read_text(encoding='utf-8'))
basket = load_basket_document(config['basket_path'])
settings = ConnectionSettings(
    host=config['host'],
    port=int(config['port']),
    client_id=int(config['client_id']),
    account=str(config.get('account') or ''),
)
component_rows = [r for r in basket.rows if r.symbol.upper() != 'XOP' and r.quantity > 0]
symbols = tuple(sorted({r.symbol for r in component_rows} | {'XOP'}))
with ib_connection(settings) as ib:
    snap = _snapshot(ib, settings)
    states = {s.symbol: s for s in _load_symbol_market_states(ib, symbols)}

print('snapshot', snap.server_time, snap.active_account)
print('basket_rows', len(basket.rows), 'component_rows', len(component_rows))
print('component_qty', sum(r.quantity for r in component_rows))
print('xop_qty', next((r.quantity for r in basket.rows if r.symbol.upper() == 'XOP'), 990))
print('symbol,qty,bid,ask,last,mid,spread')
for r in sorted(component_rows, key=lambda x: x.symbol):
    s = states.get(r.symbol)
    bid = s.bid if s else 0.0
    ask = s.ask if s else 0.0
    last = s.last if s else 0.0
    mid = (bid + ask) / 2 if bid and ask else 0.0
    spread = ask - bid if bid and ask else 0.0
    print(f"{r.symbol},{r.quantity},{bid},{ask},{last},{mid},{spread}")
s = states.get('XOP')
bid = s.bid if s else 0.0
ask = s.ask if s else 0.0
last = s.last if s else 0.0
mid = (bid + ask) / 2 if bid and ask else 0.0
spread = ask - bid if bid and ask else 0.0
print(f"XOP,{next((r.quantity for r in basket.rows if r.symbol.upper() == 'XOP'), 990)},{bid},{ask},{last},{mid},{spread}")
