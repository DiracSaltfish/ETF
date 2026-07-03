from pathlib import Path
import json
from collections import defaultdict

from ib_insync import LimitOrder, Stock

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

component_targets = {}
xop_target = 0
for row in basket.rows:
    if row.symbol.upper() == 'XOP':
        xop_target += row.signed_target
    else:
        component_targets[row.symbol.upper()] = component_targets.get(row.symbol.upper(), 0) + row.signed_target
all_symbols = tuple(sorted(set(component_targets) | {'XOP'}))

with ib_connection(settings) as ib:
    snapshot = _snapshot(ib, settings)
    positions = _load_portfolio_positions(ib, snapshot.active_account)
    market_states = {s.symbol: s for s in _load_symbol_market_states(ib, all_symbols)}
    summary_rows = ib.accountSummary(account=snapshot.active_account)

    position_map = defaultdict(float)
    for p in positions:
        position_map[p.symbol.upper()] += float(p.quantity)

    def px(sym: str) -> float:
        s = market_states.get(sym)
        if not s:
            return 0.0
        if s.bid and s.ask:
            return (s.bid + s.ask) / 2
        return s.market_price or s.last or s.close or 0.0

    xop_current = position_map.get('XOP', 0.0)
    xop_needed = float(xop_target) - xop_current

    current_component_short_mv = 0.0
    for sym in sorted(component_targets):
        cur = position_map.get(sym, 0.0)
        if cur < 0:
            current_component_short_mv += abs(cur) * px(sym)

    xop_990_mv = float(xop_target) * px('XOP')

    summary = {}
    for row in summary_rows:
        if row.account != snapshot.active_account:
            continue
        if row.currency not in {'BASE', 'USD'}:
            continue
        summary.setdefault(row.tag, {})[row.currency] = row.value

    def val(tag):
        raw = summary.get(tag, {}).get('BASE') or summary.get(tag, {}).get('USD')
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None

    print('snapshot_time', snapshot.server_time)
    print('account', snapshot.active_account)
    print('xop_current', xop_current)
    print('xop_target', xop_target)
    print('xop_needed_to_reach_target', xop_needed)
    print('component_short_mv_current', round(current_component_short_mv, 2))
    print('xop_990_mv_current', round(xop_990_mv, 2))
    print('hedge_diff_dollar', round(current_component_short_mv - xop_990_mv, 2))
    print('hedge_diff_pct_of_xop', round((current_component_short_mv - xop_990_mv) / xop_990_mv * 100, 4) if xop_990_mv else 0.0)

    print('current_InitMarginReq', val('InitMarginReq'))
    print('current_MaintMarginReq', val('MaintMarginReq'))
    print('current_SMA', val('SMA'))
    print('current_AvailableFunds', val('AvailableFunds'))
    print('current_ExcessLiquidity', val('ExcessLiquidity'))
    print('current_LookAheadAvailableFunds', val('LookAheadAvailableFunds'))
    print('current_LookAheadExcessLiquidity', val('LookAheadExcessLiquidity'))
    print('current_RegTEquity', val('RegTEquity'))
    print('current_RegTMargin', val('RegTMargin'))

    if xop_needed > 0:
        contract = ib.qualifyContracts(Stock('XOP', 'SMART', 'USD'))[0]
        ref_price = market_states['XOP'].ask or market_states['XOP'].last or market_states['XOP'].market_price or market_states['XOP'].close
        order = LimitOrder('BUY', int(round(xop_needed)), round(float(ref_price), 2))
        order.account = snapshot.active_account
        order.tif = 'DAY'
        order.outsideRth = False
        state = ib.whatIfOrder(contract, order)
        print('whatif_initMarginBefore', state.initMarginBefore)
        print('whatif_initMarginAfter', state.initMarginAfter)
        print('whatif_initMarginChange', state.initMarginChange)
        print('whatif_maintMarginBefore', state.maintMarginBefore)
        print('whatif_maintMarginAfter', state.maintMarginAfter)
        print('whatif_maintMarginChange', state.maintMarginChange)
        print('whatif_equityWithLoanBefore', state.equityWithLoanBefore)
        print('whatif_equityWithLoanAfter', state.equityWithLoanAfter)
        print('whatif_equityWithLoanChange', state.equityWithLoanChange)
        print('whatif_commission', state.commission)
        print('whatif_warningText', state.warningText)
    else:
        print('whatif_skipped no additional xop needed')
