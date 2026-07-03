from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from datetime import datetime

from ib_insync import IB, LimitOrder, MarketOrder, Stock, util

from basket_models import (
    BasketDocument,
    ConnectionSettings,
    ConnectionSnapshot,
    OrderMonitorRecord,
    PortfolioPosition,
    SymbolMarketState,
    SubmittedOrder,
)


@contextmanager
def ib_connection(settings: ConnectionSettings):
    created_loop: asyncio.AbstractEventLoop | None = None
    try:
        asyncio.get_event_loop_policy().get_event_loop()
    except RuntimeError:
        created_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(created_loop)

    ib = IB()
    try:
        ib.connect(
            settings.host,
            settings.port,
            clientId=settings.client_id,
            timeout=10,
            account=settings.account,
        )
        yield ib
    finally:
        if ib.isConnected():
            ib.disconnect()
        if created_loop is not None:
            asyncio.set_event_loop(None)
            created_loop.close()


def _snapshot(ib: IB, settings: ConnectionSettings) -> ConnectionSnapshot:
    managed_accounts = tuple(ib.managedAccounts())
    if settings.account and settings.account not in managed_accounts:
        raise ValueError(f"账户 {settings.account} 不在当前 TWS 管理账户中")
    active_account = settings.account or (managed_accounts[0] if len(managed_accounts) == 1 else "")
    server_time = ib.reqCurrentTime().strftime("%Y-%m-%d %H:%M:%S")
    return ConnectionSnapshot(
        host=settings.host,
        port=settings.port,
        client_id=settings.client_id,
        managed_accounts=managed_accounts,
        active_account=active_account,
        server_version=ib.client.serverVersion(),
        server_time=server_time,
    )


def test_connection(settings: ConnectionSettings) -> ConnectionSnapshot:
    with ib_connection(settings) as ib:
        return _snapshot(ib, settings)


def _safe_float(value) -> float | None:
    if value is None or util.isNan(value):
        return None
    return float(value)


def _market_price_from_ticker(ticker) -> float:
    market_price = ticker.marketPrice()
    if not util.isNan(market_price):
        return float(market_price)
    for value in (ticker.last, ticker.bid, ticker.ask, ticker.close):
        if not util.isNan(value):
            return float(value)
    return 0.0


def _load_symbol_market_states(ib: IB, symbols: tuple[str, ...]) -> tuple[SymbolMarketState, ...]:
    if not symbols:
        return ()
    contracts = [Stock(symbol, "SMART", "USD") for symbol in symbols]
    qualified = ib.qualifyContracts(*contracts)
    tickers_by_symbol = {}
    for contract in qualified:
        tickers_by_symbol[contract.symbol.upper()] = ib.reqMktData(
            contract,
            genericTickList="236",
            snapshot=False,
            regulatorySnapshot=False,
        )

    deadline = time.time() + 5.0
    while time.time() < deadline:
        ib.sleep(0.25)

    states: list[SymbolMarketState] = []
    for symbol, ticker in tickers_by_symbol.items():
        states.append(
            SymbolMarketState(
                symbol=symbol,
                market_price=_market_price_from_ticker(ticker),
                bid=_safe_float(ticker.bid) or 0.0,
                ask=_safe_float(ticker.ask) or 0.0,
                last=_safe_float(ticker.last) or 0.0,
                close=_safe_float(ticker.close) or 0.0,
                shortable_shares=_safe_float(ticker.shortableShares),
            )
        )
        ib.cancelMktData(ticker.contract)
    states.sort(key=lambda item: item.symbol)
    return tuple(states)


def _load_portfolio_positions(ib: IB, active_account: str) -> tuple[PortfolioPosition, ...]:
    items = ib.portfolio(account=active_account) if active_account else ib.portfolio()
    positions = tuple(
        PortfolioPosition(
            account=item.account,
            symbol=item.contract.symbol,
            local_symbol=item.contract.localSymbol,
            sec_type=item.contract.secType,
            exchange=item.contract.primaryExchange or item.contract.exchange,
            currency=item.contract.currency,
            quantity=float(item.position),
            avg_cost=float(item.averageCost),
            market_price=float(item.marketPrice),
            market_value=float(item.marketValue),
            unrealized_pnl=float(item.unrealizedPNL),
            realized_pnl=float(item.realizedPNL),
        )
        for item in items
        if item.contract.secType == "STK"
    )
    return tuple(sorted(positions, key=lambda item: item.symbol))


def load_positions(
    settings: ConnectionSettings,
) -> tuple[ConnectionSnapshot, tuple[PortfolioPosition, ...]]:
    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        positions = _load_portfolio_positions(ib, snapshot.active_account)
        return snapshot, positions


def load_market_states(
    settings: ConnectionSettings,
    symbols: tuple[str, ...] = (),
) -> tuple[ConnectionSnapshot, tuple[SymbolMarketState, ...]]:
    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        market_states = _load_symbol_market_states(ib, tuple(sorted(set(symbols))))
        return snapshot, market_states


def load_ib_state(
    settings: ConnectionSettings,
    symbols: tuple[str, ...] = (),
) -> tuple[ConnectionSnapshot, tuple[PortfolioPosition, ...], tuple[SymbolMarketState, ...]]:
    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        positions = _load_portfolio_positions(ib, snapshot.active_account)
        market_states = _load_symbol_market_states(ib, tuple(sorted(set(symbols))))
        return snapshot, positions, market_states


def _pick_sell_limit_price_from_state(state: SymbolMarketState, buffer_bps: int) -> float:
    reference = state.bid or state.last or state.close or state.market_price
    if not reference or reference <= 0:
        raise ValueError(f"{state.symbol} 没有可用报价，无法生成限价单")
    if state.bid:
        return round(state.bid, 2)
    adjusted = reference * (1 - buffer_bps / 10_000.0)
    return round(max(adjusted, 0.01), 2)


def _pick_opponent_limit_price_from_state(state: SymbolMarketState, action: str) -> float:
    action = action.upper()
    if action == "BUY":
        reference = state.ask or state.last or state.close or state.market_price or state.bid
    else:
        reference = state.bid or state.last or state.close or state.market_price or state.ask
    if not reference or reference <= 0:
        raise ValueError(f"{state.symbol} 没有可用盘口，无法生成对手价限价单")
    return round(reference, 2)


def _component_rows(basket: BasketDocument, base_symbol: str = "XOP"):
    base_symbol = base_symbol.upper()
    return tuple(
        item
        for item in basket.rows
        if item.symbol.upper() != base_symbol and item.quantity > 0
    )


def place_sell_basket_orders(
    settings: ConnectionSettings,
    basket: BasketDocument,
    *,
    order_type: str,
    tif: str,
    outside_rth: bool,
    limit_buffer_bps: int,
) -> tuple[ConnectionSnapshot, tuple[SubmittedOrder, ...]]:
    if not settings.account:
        raise ValueError("下单前必须明确选择 IBKR 账户")

    sell_rows = tuple(item for item in basket.sell_rows if item.quantity > 0)
    if not sell_rows:
        raise ValueError("当前篮子没有 SELL 行可供卖出")

    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        portfolio_items = ib.portfolio(account=snapshot.active_account)
        long_inventory: dict[str, float] = {}
        for item in portfolio_items:
            symbol = item.contract.symbol.upper()
            long_inventory[symbol] = long_inventory.get(symbol, 0.0) + float(item.position)
        contracts = [Stock(item.symbol, "SMART", "USD") for item in sell_rows]
        qualified = ib.qualifyContracts(*contracts)
        contract_map = {contract.symbol.upper(): contract for contract in qualified}
        market_state_map = {state.symbol: state for state in _load_symbol_market_states(ib, tuple(item.symbol for item in sell_rows))}
        for item in sell_rows:
            if item.symbol.upper() not in contract_map:
                raise ValueError(f"{item.symbol} 合约识别失败")
            symbol = item.symbol.upper()
            long_available = max(long_inventory.get(symbol, 0.0), 0.0)
            market_state = market_state_map.get(symbol)
            shortable_shares = market_state.shortable_shares if market_state else None
            borrow_capacity = max(shortable_shares, 0.0) if shortable_shares is not None else 0.0
            total_capacity = long_available + borrow_capacity
            if total_capacity + 1e-9 < item.quantity:
                if shortable_shares is None:
                    raise ValueError(f"{item.symbol} 未返回可融券数量，且现货库存不足: 需要 {item.quantity}，现货 {long_available:g}")
                raise ValueError(
                    f"{item.symbol} 可执行数量不足: 需要 {item.quantity}，现货 {long_available:g}，可融 {borrow_capacity:g}"
                )

        limit_prices: dict[str, float] = {}
        if order_type.upper() == "LMT":
            for item in sell_rows:
                symbol = item.symbol.upper()
                market_state = market_state_map.get(symbol)
                if market_state is None:
                    raise ValueError(f"{symbol} 没有拿到可用行情，无法生成限价单")
                limit_prices[symbol] = _pick_sell_limit_price_from_state(market_state, limit_buffer_bps)

        submitted: list[SubmittedOrder] = []
        for item in sell_rows:
            symbol = item.symbol.upper()
            contract = contract_map[symbol]
            if order_type.upper() == "MKT":
                order = MarketOrder("SELL", item.quantity)
                limit_price = None
            else:
                limit_price = limit_prices[symbol]
                order = LimitOrder("SELL", item.quantity, limit_price)
            order.tif = tif.upper()
            order.outsideRth = bool(outside_rth)
            order.account = settings.account
            trade = ib.placeOrder(contract, order)
            ib.sleep(0.2)
            status = trade.orderStatus.status or "Submitted"
            order_id = int(trade.order.orderId or 0)
            perm_id = int(trade.order.permId or 0)
            submitted.append(
                SubmittedOrder(
                    symbol=symbol,
                    action="SELL",
                    quantity=item.quantity,
                    order_type=order.orderType,
                    tif=order.tif,
                    limit_price=limit_price,
                    order_id=order_id,
                    perm_id=perm_id,
                    status=status,
                )
            )
        return snapshot, tuple(submitted)


def place_component_basket_orders(
    settings: ConnectionSettings,
    basket: BasketDocument,
    *,
    action: str,
    pricing_mode: str,
    tif: str,
    outside_rth: bool,
    base_symbol: str = "XOP",
) -> tuple[ConnectionSnapshot, tuple[SubmittedOrder, ...]]:
    if not settings.account:
        raise ValueError("下单前必须明确选择 IBKR 账户")

    action = action.upper()
    pricing_mode = pricing_mode.upper()
    rows = _component_rows(basket, base_symbol=base_symbol)
    if not rows:
        raise ValueError(f"当前篮子没有除 {base_symbol} 以外的成分股行")

    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        contracts = [Stock(item.symbol, "SMART", "USD") for item in rows]
        qualified = ib.qualifyContracts(*contracts)
        contract_map = {contract.symbol.upper(): contract for contract in qualified}
        market_state_map = {state.symbol: state for state in _load_symbol_market_states(ib, tuple(item.symbol for item in rows))}

        if action == "SELL":
            portfolio_items = ib.portfolio(account=snapshot.active_account)
            long_inventory: dict[str, float] = {}
            for item in portfolio_items:
                symbol = item.contract.symbol.upper()
                long_inventory[symbol] = long_inventory.get(symbol, 0.0) + float(item.position)
            for item in rows:
                symbol = item.symbol.upper()
                if symbol not in contract_map:
                    raise ValueError(f"{item.symbol} 合约识别失败")
                long_available = max(long_inventory.get(symbol, 0.0), 0.0)
                market_state = market_state_map.get(symbol)
                shortable_shares = market_state.shortable_shares if market_state else None
                borrow_capacity = max(shortable_shares, 0.0) if shortable_shares is not None else 0.0
                total_capacity = long_available + borrow_capacity
                if total_capacity + 1e-9 < item.quantity:
                    if shortable_shares is None:
                        raise ValueError(f"{item.symbol} 未返回可融券数量，且现货库存不足: 需要 {item.quantity}，现货 {long_available:g}")
                    raise ValueError(
                        f"{item.symbol} 可执行数量不足: 需要 {item.quantity}，现货 {long_available:g}，可融 {borrow_capacity:g}"
                    )

        submitted: list[SubmittedOrder] = []
        for item in rows:
            symbol = item.symbol.upper()
            contract = contract_map.get(symbol)
            if contract is None:
                raise ValueError(f"{symbol} 合约识别失败")
            limit_price = None
            if pricing_mode == "MKT":
                order = MarketOrder(action, item.quantity)
            elif pricing_mode == "OPPONENT":
                market_state = market_state_map.get(symbol)
                if market_state is None:
                    raise ValueError(f"{symbol} 没有拿到可用行情，无法生成对手价订单")
                limit_price = _pick_opponent_limit_price_from_state(market_state, action)
                order = LimitOrder(action, item.quantity, limit_price)
            else:
                raise ValueError(f"不支持的成分股定价模式: {pricing_mode}")
            order.tif = tif.upper()
            order.outsideRth = bool(outside_rth)
            order.account = settings.account
            trade = ib.placeOrder(contract, order)
            ib.sleep(0.2)
            submitted.append(
                SubmittedOrder(
                    symbol=symbol,
                    action=action,
                    quantity=item.quantity,
                    order_type=order.orderType,
                    tif=order.tif,
                    limit_price=limit_price,
                    order_id=int(trade.order.orderId or 0),
                    perm_id=int(trade.order.permId or 0),
                    status=trade.orderStatus.status or "Submitted",
                )
            )
        return snapshot, tuple(submitted)


def place_single_symbol_order(
    settings: ConnectionSettings,
    *,
    symbol: str,
    action: str,
    quantity: int,
    order_type: str,
    tif: str,
    outside_rth: bool,
    limit_price: float | None = None,
) -> tuple[ConnectionSnapshot, SubmittedOrder]:
    if not settings.account:
        raise ValueError("下单前必须明确选择 IBKR 账户")
    if quantity <= 0:
        raise ValueError("下单数量必须大于 0")
    action = action.upper()
    order_type = order_type.upper()
    symbol = symbol.upper().strip()

    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        contracts = ib.qualifyContracts(Stock(symbol, "SMART", "USD"))
        if not contracts:
            raise ValueError(f"{symbol} 合约识别失败")
        contract = contracts[0]
        if order_type == "MKT":
            order = MarketOrder(action, quantity)
            actual_limit = None
        elif order_type == "OPPONENT":
            market_state_map = {state.symbol: state for state in _load_symbol_market_states(ib, (symbol,))}
            market_state = market_state_map.get(symbol)
            if market_state is None:
                raise ValueError(f"{symbol} 没有拿到可用行情，无法生成对手价订单")
            actual_limit = _pick_opponent_limit_price_from_state(market_state, action)
            order = LimitOrder(action, quantity, actual_limit)
        elif order_type == "LMT":
            if limit_price is None or limit_price <= 0:
                raise ValueError("限价单必须提供大于 0 的价格")
            actual_limit = round(float(limit_price), 4)
            order = LimitOrder(action, quantity, actual_limit)
        else:
            raise ValueError(f"不支持的订单类型: {order_type}")
        order.tif = tif.upper()
        order.outsideRth = bool(outside_rth)
        order.account = settings.account
        trade = ib.placeOrder(contract, order)
        ib.sleep(0.2)
        submitted = SubmittedOrder(
            symbol=symbol,
            action=action,
            quantity=quantity,
            order_type=order.orderType,
            tif=order.tif,
            limit_price=actual_limit,
            order_id=int(trade.order.orderId or 0),
            perm_id=int(trade.order.permId or 0),
            status=trade.orderStatus.status or "Submitted",
        )
        return snapshot, submitted


def cancel_monitor_orders(
    settings: ConnectionSettings,
    tracked_orders: tuple[OrderMonitorRecord, ...],
) -> tuple[ConnectionSnapshot, tuple[tuple[str, str], ...]]:
    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        open_trades = ib.reqAllOpenOrders()
        trade_by_perm: dict[int, object] = {}
        trade_by_order: dict[int, object] = {}
        for trade in open_trades:
            perm_id = int(trade.order.permId or trade.orderStatus.permId or 0)
            order_id = int(trade.order.orderId or 0)
            if perm_id:
                trade_by_perm[perm_id] = trade
            if order_id:
                trade_by_order[order_id] = trade

        messages: list[tuple[str, str]] = []
        for record in tracked_orders:
            trade = trade_by_perm.get(record.perm_id) or trade_by_order.get(record.order_id)
            if trade is None:
                messages.append((record.symbol, "未找到活动订单，无法撤单"))
                continue
            ib.cancelOrder(trade.order)
            messages.append((record.symbol, "已发送撤单请求"))
        ib.sleep(0.5)
        return snapshot, tuple(messages)


def refresh_order_monitor(
    settings: ConnectionSettings,
    tracked_orders: tuple[OrderMonitorRecord, ...],
) -> tuple[ConnectionSnapshot, tuple[OrderMonitorRecord, ...]]:
    with ib_connection(settings) as ib:
        snapshot = _snapshot(ib, settings)
        open_trades = ib.reqAllOpenOrders()
        completed_trades = ib.reqCompletedOrders(False)
        fills = ib.reqExecutions()

        trade_by_perm: dict[int, object] = {}
        trade_by_order: dict[int, object] = {}
        for trade in list(open_trades) + list(completed_trades):
            perm_id = int(trade.order.permId or trade.orderStatus.permId or 0)
            order_id = int(trade.order.orderId or 0)
            if perm_id:
                trade_by_perm[perm_id] = trade
            if order_id:
                trade_by_order[order_id] = trade

        fills_by_perm: dict[int, list[object]] = {}
        fills_by_order: dict[int, list[object]] = {}
        for fill in fills:
            perm_id = int(fill.execution.permId or 0)
            order_id = int(fill.execution.orderId or 0)
            if perm_id:
                fills_by_perm.setdefault(perm_id, []).append(fill)
            if order_id:
                fills_by_order.setdefault(order_id, []).append(fill)

        updated_records: list[OrderMonitorRecord] = []
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for record in tracked_orders:
            trade = trade_by_perm.get(record.perm_id) or trade_by_order.get(record.order_id)
            related_fills = fills_by_perm.get(record.perm_id) or fills_by_order.get(record.order_id) or []
            filled_qty = record.filled
            avg_fill_price = record.avg_fill_price
            if related_fills:
                total_filled = sum(float(fill.execution.shares) for fill in related_fills)
                total_value = sum(float(fill.execution.shares) * float(fill.execution.price) for fill in related_fills)
                filled_qty = total_filled
                avg_fill_price = (total_value / total_filled) if total_filled else 0.0
            status = record.status
            remaining = max(float(record.quantity) - filled_qty, 0.0)
            note = record.note
            if trade is not None:
                status = trade.orderStatus.status or status
                filled_qty = float(trade.orderStatus.filled or filled_qty or 0.0)
                remaining = float(trade.orderStatus.remaining if trade.orderStatus.remaining or trade.orderStatus.remaining == 0 else max(float(record.quantity) - filled_qty, 0.0))
                avg_fill_price = float(trade.orderStatus.avgFillPrice or avg_fill_price or 0.0)
                note = trade.advancedError or note
            elif related_fills:
                status = "Filled" if filled_qty + 1e-9 >= float(record.quantity) else "PartiallyFilled"
            updated_records.append(
                OrderMonitorRecord(
                    batch_id=record.batch_id,
                    group_label=record.group_label,
                    submitted_at=record.submitted_at,
                    symbol=record.symbol,
                    action=record.action,
                    quantity=record.quantity,
                    order_type=record.order_type,
                    limit_price=record.limit_price,
                    order_id=record.order_id,
                    perm_id=record.perm_id,
                    status=status,
                    filled=filled_qty,
                    remaining=remaining,
                    avg_fill_price=avg_fill_price,
                    last_update=now_text,
                    note=note or "",
                )
            )
        return snapshot, tuple(updated_records)
