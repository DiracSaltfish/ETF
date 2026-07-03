from pathlib import Path
import json
from collections import defaultdict
import pandas as pd

from basket_models import ConnectionSettings
from ib_service import ib_connection, _snapshot, _load_portfolio_positions

basket_path = Path('/Users/ellis/Desktop/ETF交割/篮子管理/xop_990_share_short_basket_live_2026-07-02.xlsx')
config = json.loads(Path('/Users/ellis/Desktop/ETF交割/篮子管理/config.json').read_text(encoding='utf-8'))
settings = ConnectionSettings(config['host'], int(config['port']), int(config['client_id']), str(config.get('account') or ''))

summary = pd.read_excel(basket_path, sheet_name='Summary')
basket = pd.read_excel(basket_path, sheet_name='Basket')
base_target = int(summary.loc[summary['metric']=='target_xop_shares','value'].iloc[0])
base_xop_price = float(summary.loc[summary['metric']=='xop_price','value'].iloc[0])
weights = basket[['ticker','name','weight_pct','price','target_short_shares']].copy()
weights['ticker']=weights['ticker'].astype(str).str.upper().str.strip()
weights['weight_pct']=pd.to_numeric(weights['weight_pct'], errors='coerce')
weights['price']=pd.to_numeric(weights['price'], errors='coerce')
weights['target_short_shares']=pd.to_numeric(weights['target_short_shares'], errors='coerce').astype(int)

target_xop_shares = 2000
target_nominal = base_xop_price * target_xop_shares
weights['target_short_shares_2000']=(target_nominal*(weights['weight_pct']/100.0)/weights['price']).round().astype(int)
with ib_connection(settings) as ib:
    snapshot = _snapshot(ib, settings)
    positions = _load_portfolio_positions(ib, snapshot.active_account)
position_map = defaultdict(float)
for p in positions:
    position_map[p.symbol.upper()] += float(p.quantity)
weights['current_short']=weights['ticker'].map(lambda s: max(-position_map.get(s,0.0),0.0))
weights['additional_sell_shares']=(weights['target_short_shares_2000']-weights['current_short']).clip(lower=0).round().astype(int)
weights['cover_needed_shares']=(weights['current_short']-weights['target_short_shares_2000']).clip(lower=0).round().astype(int)
weights['plan_basis']='current_program_basket_file'
weights['base_target_xop_shares']=base_target
weights['target_xop_shares']=target_xop_shares
weights['snapshot_time']=snapshot.server_time
out=weights[['ticker','name','weight_pct','price','target_short_shares','target_short_shares_2000','current_short','additional_sell_shares','cover_needed_shares','plan_basis','base_target_xop_shares','target_xop_shares','snapshot_time']]
out_path=Path('/Users/ellis/Desktop/ETF交割/tmp/xop_2000_component_plan_current_file.csv')
out.to_csv(out_path,index=False)
print(out_path)
