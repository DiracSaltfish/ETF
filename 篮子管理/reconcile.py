from __future__ import annotations

from basket_models import BasketDocument, PortfolioPosition, ReconciliationRow, SymbolMarketState


def reconcile_basket(
    basket: BasketDocument,
    positions: tuple[PortfolioPosition, ...],
    market_states: tuple[SymbolMarketState, ...] = (),
) -> tuple[ReconciliationRow, ...]:
    position_map: dict[str, float] = {}
    market_price_map: dict[str, float] = {}
    market_value_map: dict[str, float] = {}
    account_map: dict[str, str] = {}
    for item in positions:
        position_map[item.symbol] = position_map.get(item.symbol, 0.0) + float(item.quantity)
        market_value_map[item.symbol] = market_value_map.get(item.symbol, 0.0) + float(item.market_value)
        if item.market_price:
            market_price_map[item.symbol] = float(item.market_price)
        if item.account and item.symbol not in account_map:
            account_map[item.symbol] = item.account
    market_state_map = {item.symbol: item for item in market_states}

    rows: list[ReconciliationRow] = []
    for basket_item in basket.rows:
        current_position = position_map.get(basket_item.symbol, 0.0)
        target_position = float(basket_item.signed_target)
        delta_to_target = target_position - current_position
        market_state = market_state_map.get(basket_item.symbol)
        long_inventory = max(current_position, 0.0) if basket_item.action == "SELL" else 0.0
        shortable_shares = market_state.shortable_shares if market_state else None
        borrow_capacity = max(shortable_shares, 0.0) if shortable_shares is not None else 0.0
        execution_capacity = long_inventory + borrow_capacity if basket_item.action == "SELL" else 0.0
        execution_shortfall = (
            max(0.0, float(basket_item.quantity) - execution_capacity)
            if basket_item.action == "SELL"
            else 0.0
        )
        if abs(delta_to_target) < 1e-9:
            target_status = "已满足"
        elif delta_to_target > 0:
            target_status = "需买入"
        else:
            target_status = "需卖出"
        if basket_item.action == "SELL":
            if long_inventory + 1e-9 >= basket_item.quantity:
                sell_status = "现货可卖"
                note = "现有多头库存足够，直接卖出现货即可。"
            elif shortable_shares is None:
                sell_status = "待查券源"
                note = "现货库存不足，且 IB 未返回 shortableShares，暂时无法确认能否卖空。"
            elif execution_shortfall <= 1e-9:
                sell_status = "可融可卖空"
                note = (
                    f"现货库存 {long_inventory:,.0f} 股不足时，可使用 IB 可融数量 "
                    f"{borrow_capacity:,.0f} 股补足卖空。"
                )
            else:
                sell_status = "融券不足"
                note = (
                    f"现货库存 {long_inventory:,.0f} 股 + 可融数量 {borrow_capacity:,.0f} 股"
                    f" 仍不足覆盖 {basket_item.quantity:,.0f} 股。"
                )
        else:
            sell_status = "不参与"
            note = "BUY 行只参与目标持仓匹配，不参与一键卖出。"
        rows.append(
            ReconciliationRow(
                item=basket_item,
                current_position=current_position,
                target_position=target_position,
                delta_to_target=delta_to_target,
                long_inventory=long_inventory,
                shortable_shares=shortable_shares,
                execution_capacity=execution_capacity,
                execution_shortfall=execution_shortfall,
                market_price=(market_state.market_price if market_state and market_state.market_price else market_price_map.get(basket_item.symbol, 0.0)),
                market_value=market_value_map.get(basket_item.symbol, 0.0),
                account=account_map.get(basket_item.symbol, ""),
                target_status=target_status,
                sell_status=sell_status,
                note=note,
            )
        )
    return tuple(rows)
