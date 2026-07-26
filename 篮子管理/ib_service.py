from __future__ import annotations

import asyncio
import time
from contextlib import contextmanager
from dataclasses import dataclass, replace
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
from close_only import (
    ActiveOrderSnapshot,
    CloseCampaignBasis,
    CloseOnlyPlan,
    basket_component_symbols,
    build_close_only_plan,
    compute_base_sell_due,
    plans_match_for_execution,
)
from close_store import ensure_campaign, mark_campaign_complete, record_event, record_submitted_order


@dataclass(frozen=True)
class CloseOnlyPreview:
    snapshot: ConnectionSnapshot
    positions: tuple[PortfolioPosition, ...]
    market_states: tuple[SymbolMarketState, ...]
    active_orders: tuple[ActiveOrderSnapshot, ...]
    plan: CloseOnlyPlan


@dataclass(frozen=True)
class CloseOnlyExecutionResult:
    snapshot: ConnectionSnapshot
    plan: CloseOnlyPlan
    orders: tuple[SubmittedOrder, ...]
    positions_after: tuple[PortfolioPosition, ...]
    status: str
    error: str = ""


@contextmanager
def ib_connection(settings: ConnectionSettings, *, readonly: bool = False):
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
            readonly=readonly,
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
            con_id=int(item.contract.conId or 0),
        )
        for item in items
        if item.contract.secType == "STK"
    )
    return tuple(sorted(positions, key=lambda item: item.symbol))


def _load_active_order_snapshots(
    ib: IB,
    active_account: str,
) -> tuple[ActiveOrderSnapshot, ...]:
    records: list[ActiveOrderSnapshot] = []
    for trade in ib.reqAllOpenOrders():
        order = trade.order
        status = trade.orderStatus
        account = str(order.account or "")
        if active_account and account != active_account:
            continue
        total_quantity = float(order.totalQuantity or 0.0)
        filled = float(status.filled or 0.0)
        raw_remaining = status.remaining
        remaining = (
            float(raw_remaining)
            if raw_remaining is not None and not util.isNan(raw_remaining)
            else max(total_quantity - filled, 0.0)
        )
        records.append(
            ActiveOrderSnapshot(
                account=account,
                con_id=int(trade.contract.conId or 0),
                symbol=str(trade.contract.symbol or "").upper(),
                action=str(order.action or "").upper(),
                total_quantity=total_quantity,
                filled=filled,
                remaining=remaining,
                status=str(status.status or "Unknown"),
                order_id=int(order.orderId or 0),
                perm_id=int(order.permId or status.permId or 0),
                order_ref=str(order.orderRef or ""),
            )
        )
    return tuple(records)


def _close_scope_symbols(
    basket: BasketDocument,
    *,
    base_symbol: str,
) -> tuple[str, ...]:
    return tuple(
        sorted(set(basket_component_symbols(basket, base_symbol=base_symbol)) | {base_symbol.upper()})
    )


def _validate_qualified_contracts(
    plan: CloseOnlyPlan,
    contract_map: dict[str, object],
) -> CloseOnlyPlan:
    blockers = list(plan.blockers)
    for line in plan.lines:
        contract = contract_map.get(line.symbol)
        if contract is None:
            blockers.append(f"{line.symbol} 合约识别失败")
            continue
        qualified_con_id = int(contract.conId or 0)
        if qualified_con_id != line.con_id:
            blockers.append(
                f"{line.symbol} 合约不一致: 持仓 conId={line.con_id} / "
                f"SMART 识别 conId={qualified_con_id}"
            )
    return replace(plan, blockers=tuple(dict.fromkeys(blockers)))


def _load_close_only_state(
    ib: IB,
    settings: ConnectionSettings,
    basket: BasketDocument,
    *,
    tranche_percent: int,
    base_symbol: str,
    campaign_basis: CloseCampaignBasis | None,
):
    snapshot = _snapshot(ib, settings)
    symbols = _close_scope_symbols(basket, base_symbol=base_symbol)
    contracts = [Stock(symbol, "SMART", "USD") for symbol in symbols]
    qualified = ib.qualifyContracts(*contracts)
    contract_map = {contract.symbol.upper(): contract for contract in qualified}
    market_states = _load_symbol_market_states(ib, symbols)
    # Positions and open orders are intentionally loaded last so the approval
    # signature reflects the freshest state after market-data preparation.
    positions = _load_portfolio_positions(ib, snapshot.active_account)
    active_orders = _load_active_order_snapshots(ib, snapshot.active_account)
    plan = build_close_only_plan(
        basket,
        positions,
        account=snapshot.active_account,
        active_orders=active_orders,
        tranche_percent=tranche_percent,
        base_symbol=base_symbol,
        campaign_basis=campaign_basis,
    )
    plan = _validate_qualified_contracts(plan, contract_map)
    return snapshot, positions, market_states, active_orders, contract_map, plan


def load_close_only_preview(
    settings: ConnectionSettings,
    basket: BasketDocument,
    *,
    tranche_percent: int = 25,
    base_symbol: str = "XOP",
    campaign_basis: CloseCampaignBasis | None = None,
    pricing_mode: str = "OPPONENT",
) -> CloseOnlyPreview:
    if not settings.account:
        raise ValueError("生成 Close Only 计划前必须明确选择 IBKR 账户")
    with ib_connection(settings, readonly=True) as ib:
        snapshot, positions, market_states, active_orders, _, plan = _load_close_only_state(
            ib,
            settings,
            basket,
            tranche_percent=tranche_percent,
            base_symbol=base_symbol,
            campaign_basis=campaign_basis,
        )
        plan = _with_close_pricing_blockers(plan, market_states, pricing_mode)
        if campaign_basis is not None and not plan.lines and not plan.blockers and not plan.active_orders:
            try:
                mark_campaign_complete(campaign_basis.campaign_id)
            except Exception as exc:
                plan = replace(
                    plan,
                    blockers=plan.blockers + (f"完成状态写入失败: {exc}",),
                )
        return CloseOnlyPreview(
            snapshot=snapshot,
            positions=positions,
            market_states=market_states,
            active_orders=active_orders,
            plan=plan,
        )


def _build_close_order(
    *,
    action: str,
    quantity: int,
    pricing_mode: str,
    market_state: SymbolMarketState | None,
    account: str,
    tif: str,
    outside_rth: bool,
    order_ref: str,
):
    pricing_mode = pricing_mode.upper()
    if pricing_mode == "MKT":
        order = MarketOrder(action, quantity)
        limit_price = None
    elif pricing_mode == "OPPONENT":
        if market_state is None:
            raise ValueError("没有拿到可用行情，无法生成盘口对手价订单")
        limit_price = _pick_opponent_limit_price_from_state(market_state, action)
        order = LimitOrder(action, quantity, limit_price)
    else:
        raise ValueError(f"不支持的 Close Only 定价模式: {pricing_mode}")
    order.tif = tif.upper()
    order.outsideRth = bool(outside_rth)
    order.account = account
    order.orderRef = order_ref
    order.transmit = True
    return order, limit_price


def _with_close_pricing_blockers(
    plan: CloseOnlyPlan,
    market_states: tuple[SymbolMarketState, ...],
    pricing_mode: str,
) -> CloseOnlyPlan:
    blockers = list(plan.blockers)
    mode = pricing_mode.upper()
    if mode not in {"MKT", "OPPONENT"}:
        blockers.append(f"不支持的 Close Only 定价模式: {pricing_mode}")
    elif mode == "OPPONENT":
        market_map = {state.symbol: state for state in market_states}
        for line in plan.lines:
            state = market_map.get(line.symbol)
            if state is None:
                blockers.append(f"{line.symbol} 没有拿到行情，无法生成盘口对手价")
                continue
            try:
                _pick_opponent_limit_price_from_state(state, line.action)
            except ValueError as exc:
                blockers.append(str(exc))
    return replace(plan, blockers=tuple(dict.fromkeys(blockers)))


def _submitted_order_from_trade(
    *,
    trade,
    symbol: str,
    con_id: int,
    action: str,
    quantity: int,
    limit_price: float | None,
    order_ref: str,
) -> SubmittedOrder:
    return SubmittedOrder(
        symbol=symbol,
        action=action,
        quantity=quantity,
        order_type=trade.order.orderType,
        tif=trade.order.tif,
        limit_price=limit_price,
        order_id=int(trade.order.orderId or 0),
        perm_id=int(trade.order.permId or trade.orderStatus.permId or 0),
        status=trade.orderStatus.status or "Submitted",
        con_id=con_id,
        order_ref=order_ref,
    )


def execute_close_only_plan(
    settings: ConnectionSettings,
    basket: BasketDocument,
    approved_plan: CloseOnlyPlan,
    *,
    pricing_mode: str,
    tif: str,
    outside_rth: bool,
) -> CloseOnlyExecutionResult:
    if not settings.account or settings.account != approved_plan.account:
        raise ValueError("Close Only 计划账户与当前设置账户不一致")
    if outside_rth:
        raise ValueError("Close Only 默认禁止 Outside RTH；请在正常交易时段执行")
    if tif.upper() != "DAY":
        raise ValueError("Close Only 仅允许 DAY；GTC 会把未完成风险带到下一交易日")

    with ib_connection(settings) as ib:
        snapshot, initial_positions, market_states, _, contract_map, fresh_plan = _load_close_only_state(
            ib,
            settings,
            basket,
            tranche_percent=approved_plan.tranche_percent,
            base_symbol=approved_plan.base_symbol,
            campaign_basis=approved_plan.basis,
        )
        if not plans_match_for_execution(approved_plan, fresh_plan):
            raise ValueError(
                "持仓、活动订单或合约已发生变化，原 Close Only 计划已失效；"
                "请刷新预览并重新确认"
            )
        if not fresh_plan.can_execute:
            raise ValueError("Close Only 实时预检未通过")

        priced_plan = _with_close_pricing_blockers(fresh_plan, market_states, pricing_mode)
        if priced_plan.blockers:
            raise ValueError("Close Only 定价预检未通过: " + "；".join(priced_plan.blockers))

        ensure_campaign(fresh_plan, basket_path=str(basket.path))
        record_event(
            fresh_plan.basis.campaign_id,
            event="SUBMITTING",
            message=f"plan={fresh_plan.plan_id} tranche={fresh_plan.tranche_percent}%",
            status="SUBMITTING",
        )
        market_map = {state.symbol: state for state in market_states}
        submitted: list[SubmittedOrder] = []
        scope = set(_close_scope_symbols(basket, base_symbol=fresh_plan.base_symbol))
        last_known_positions = initial_positions

        def submit_line(line, sequence: int, *, quantity_override: int | None = None):
            quantity = int(quantity_override if quantity_override is not None else line.quantity)
            if quantity <= 0 or quantity > line.quantity:
                raise ValueError(f"{line.symbol} Close Only 数量越界: {quantity}/{line.quantity}")
            known_refs = {item.order_ref for item in submitted if item.order_ref}
            unexpected_orders = [
                item
                for item in _load_active_order_snapshots(ib, snapshot.active_account)
                if item.is_active
                and item.symbol in scope
                and item.order_ref not in known_refs
            ]
            if unexpected_orders:
                item = unexpected_orders[0]
                raise ValueError(
                    f"逐单校验发现新的活动订单 {item.symbol} {item.action} "
                    f"status={item.status} orderId={item.order_id}，停止后续下单"
                )

            live_positions = [
                item
                for item in _load_portfolio_positions(ib, snapshot.active_account)
                if item.account == fresh_plan.account
                and item.symbol == line.symbol
                and int(item.con_id) == line.con_id
                and abs(item.quantity) > 1e-9
            ]
            if len(live_positions) > 1:
                raise ValueError(f"{line.symbol} 逐单校验发现重复合约持仓")
            live_position = float(live_positions[0].quantity) if live_positions else 0.0
            if abs(live_position - round(live_position)) > 1e-9:
                raise ValueError(f"{line.symbol} 逐单校验发现碎股持仓 {live_position:g}")
            basis_entry = fresh_plan.basis.entry_map.get(line.symbol)
            if basis_entry is None or abs(live_position) > abs(basis_entry.initial_position) + 1e-9:
                raise ValueError(
                    f"{line.symbol} 逐单校验发现仓位超过会话初始值，停止后续下单"
                )
            if line.action == "BUY":
                if live_position >= 0:
                    record_event(
                        fresh_plan.basis.campaign_id,
                        event="ORDER_SKIPPED",
                        message=f"{line.symbol} 已无空头，跳过 BUY",
                    )
                    return None
                live_cap = int(round(abs(live_position)))
            elif line.action == "SELL":
                if live_position <= 0:
                    record_event(
                        fresh_plan.basis.campaign_id,
                        event="ORDER_SKIPPED",
                        message=f"{line.symbol} 已无多头，跳过 SELL",
                    )
                    return None
                live_cap = int(round(live_position))
            else:
                raise ValueError(f"{line.symbol} Close Only 方向非法: {line.action}")
            quantity = min(quantity, live_cap)
            if quantity <= 0:
                return None
            order_ref = (
                f"{fresh_plan.basis.campaign_id}-{fresh_plan.plan_id[-6:]}-{sequence:03d}"
            )[:64]
            record_event(
                fresh_plan.basis.campaign_id,
                event="ORDER_INTENT",
                message=f"{line.symbol} {line.action} {quantity} ref={order_ref}",
            )
            order, limit_price = _build_close_order(
                action=line.action,
                quantity=quantity,
                pricing_mode=pricing_mode,
                market_state=market_map.get(line.symbol),
                account=fresh_plan.account,
                tif=tif,
                outside_rth=outside_rth,
                order_ref=order_ref,
            )
            contract = contract_map.get(line.symbol)
            if contract is None or int(contract.conId or 0) != line.con_id:
                raise ValueError(f"{line.symbol} 下单前 conId 二次校验失败")
            trade = ib.placeOrder(contract, order)
            submitted_order = _submitted_order_from_trade(
                trade=trade,
                symbol=line.symbol,
                con_id=line.con_id,
                action=line.action,
                quantity=quantity,
                limit_price=limit_price,
                order_ref=order_ref,
            )
            submitted.append(submitted_order)
            try:
                ib.sleep(0.2)
                submitted[-1] = _submitted_order_from_trade(
                    trade=trade,
                    symbol=line.symbol,
                    con_id=line.con_id,
                    action=line.action,
                    quantity=quantity,
                    limit_price=limit_price,
                    order_ref=order_ref,
                )
            finally:
                record_submitted_order(
                    fresh_plan.basis.campaign_id,
                    plan_id=fresh_plan.plan_id,
                    order=submitted[-1],
                )

        error_text = ""
        try:
            # Components are submitted first.  XOP is deliberately not sent at
            # the projected amount until actual component fills are observed.
            for sequence, line in enumerate(fresh_plan.component_lines, start=1):
                submit_line(line, sequence)

            ib.sleep(1.0)
            positions_after_components = _load_portfolio_positions(ib, snapshot.active_account)
            last_known_positions = positions_after_components
            actual_base_due = compute_base_sell_due(
                fresh_plan.basis,
                positions_after_components,
            )
            approved_base_limit = sum(line.quantity for line in fresh_plan.base_lines)
            base_quantity = min(actual_base_due, approved_base_limit)
            if base_quantity > 0 and fresh_plan.base_lines:
                newly_active = _load_active_order_snapshots(ib, snapshot.active_account)
                if any(
                    order.is_active and order.symbol == fresh_plan.base_symbol
                    for order in newly_active
                ):
                    raise ValueError(
                        f"检测到新的 {fresh_plan.base_symbol} 活动订单，停止自动同步基准腿"
                    )
                submit_line(
                    fresh_plan.base_lines[0],
                    len(fresh_plan.component_lines) + 1,
                    quantity_override=base_quantity,
                )
        except Exception as exc:
            error_text = str(exc)
            try:
                record_event(
                    fresh_plan.basis.campaign_id,
                    event="PAUSED",
                    message=error_text,
                    status="PAUSED",
                )
            except Exception:
                pass

        try:
            ib.sleep(0.5)
            positions_after = _load_portfolio_positions(ib, snapshot.active_account)
            last_known_positions = positions_after
            active_after = _load_active_order_snapshots(ib, snapshot.active_account)
        except Exception as exc:
            reconciliation_error = f"提交后状态复核失败: {exc}"
            error_text = (
                f"{error_text}；{reconciliation_error}" if error_text else reconciliation_error
            )
            positions_after = last_known_positions
            active_after = ()
            try:
                record_event(
                    fresh_plan.basis.campaign_id,
                    event="PAUSED",
                    message=error_text,
                    status="PAUSED",
                )
            except Exception:
                pass
        remaining_positions = [
            item
            for item in positions_after
            if item.account == fresh_plan.account
            and item.symbol in scope
            and abs(item.quantity) > 1e-9
        ]
        remaining_orders = [
            item
            for item in active_after
            if item.account == fresh_plan.account
            and item.symbol in scope
            and item.is_active
        ]
        if error_text:
            status = "PAUSED"
        elif not remaining_positions and not remaining_orders:
            try:
                mark_campaign_complete(fresh_plan.basis.campaign_id)
            except Exception as exc:
                error_text = f"完成状态写入失败: {exc}"
                status = "PAUSED"
            else:
                status = "COMPLETE"
        else:
            try:
                record_event(
                    fresh_plan.basis.campaign_id,
                    event="RECONCILING",
                    message=(
                        f"remaining_positions={len(remaining_positions)} "
                        f"active_orders={len(remaining_orders)}"
                    ),
                    status="WORKING" if remaining_orders else "RECONCILING",
                )
            except Exception as exc:
                error_text = f"复核状态写入失败: {exc}"
                status = "PAUSED"
            else:
                status = "WORKING" if remaining_orders else "RECONCILING"
        return CloseOnlyExecutionResult(
            snapshot=snapshot,
            plan=fresh_plan,
            orders=tuple(submitted),
            positions_after=positions_after,
            status=status,
            error=error_text,
        )


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


def _trade_matches_monitor_record(trade, record: OrderMonitorRecord) -> bool:
    trade_account = str(trade.order.account or "")
    if not record.account or trade_account != record.account:
        return False
    if str(trade.contract.symbol or "").upper() != record.symbol.upper():
        return False
    if str(trade.order.action or "").upper() != record.action.upper():
        return False
    if record.con_id and int(trade.contract.conId or 0) != record.con_id:
        return False
    if record.order_ref and str(trade.order.orderRef or "") != record.order_ref:
        return False
    return True


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
            if str(trade.order.account or "") != snapshot.active_account:
                continue
            perm_id = int(trade.order.permId or trade.orderStatus.permId or 0)
            order_id = int(trade.order.orderId or 0)
            if perm_id:
                trade_by_perm[perm_id] = trade
            if order_id:
                trade_by_order[order_id] = trade

        messages: list[tuple[str, str]] = []
        for record in tracked_orders:
            if not record.account or record.account != snapshot.active_account:
                messages.append((record.symbol, "监控记录账户与当前账户不一致，禁止撤单"))
                continue
            trade = trade_by_perm.get(record.perm_id) or trade_by_order.get(record.order_id)
            if trade is None or not _trade_matches_monitor_record(trade, record):
                messages.append((record.symbol, "未找到完全匹配的活动订单，无法撤单"))
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
            if str(trade.order.account or "") != snapshot.active_account:
                continue
            perm_id = int(trade.order.permId or trade.orderStatus.permId or 0)
            order_id = int(trade.order.orderId or 0)
            if perm_id:
                trade_by_perm[perm_id] = trade
            if order_id:
                trade_by_order[order_id] = trade

        fills_by_perm: dict[int, list[object]] = {}
        fills_by_order: dict[int, list[object]] = {}
        for fill in fills:
            if str(fill.execution.acctNumber or "") != snapshot.active_account:
                continue
            perm_id = int(fill.execution.permId or 0)
            order_id = int(fill.execution.orderId or 0)
            if perm_id:
                fills_by_perm.setdefault(perm_id, []).append(fill)
            if order_id:
                fills_by_order.setdefault(order_id, []).append(fill)

        updated_records: list[OrderMonitorRecord] = []
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        for record in tracked_orders:
            if not record.account or record.account != snapshot.active_account:
                updated_records.append(
                    replace(
                        record,
                        last_update=now_text,
                        note=(record.note + " | " if record.note else "")
                        + "未刷新：监控记录账户与当前账户不一致",
                    )
                )
                continue
            trade = trade_by_perm.get(record.perm_id) or trade_by_order.get(record.order_id)
            if trade is not None and not _trade_matches_monitor_record(trade, record):
                trade = None
            candidate_fills = fills_by_perm.get(record.perm_id) or fills_by_order.get(record.order_id) or []
            related_fills = [
                fill
                for fill in candidate_fills
                if str(fill.contract.symbol or "").upper() == record.symbol.upper()
                and (not record.con_id or int(fill.contract.conId or 0) == record.con_id)
            ]
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
            resolved_order_id = record.order_id
            resolved_perm_id = record.perm_id
            resolved_con_id = record.con_id
            resolved_order_ref = record.order_ref
            if trade is not None:
                status = trade.orderStatus.status or status
                filled_qty = float(trade.orderStatus.filled or filled_qty or 0.0)
                remaining = float(trade.orderStatus.remaining if trade.orderStatus.remaining or trade.orderStatus.remaining == 0 else max(float(record.quantity) - filled_qty, 0.0))
                avg_fill_price = float(trade.orderStatus.avgFillPrice or avg_fill_price or 0.0)
                note = trade.advancedError or note
                resolved_order_id = int(trade.order.orderId or resolved_order_id or 0)
                resolved_perm_id = int(
                    trade.order.permId or trade.orderStatus.permId or resolved_perm_id or 0
                )
                resolved_con_id = int(trade.contract.conId or resolved_con_id or 0)
                resolved_order_ref = str(trade.order.orderRef or resolved_order_ref or "")
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
                    order_id=resolved_order_id,
                    perm_id=resolved_perm_id,
                    status=status,
                    filled=filled_qty,
                    remaining=remaining,
                    avg_fill_price=avg_fill_price,
                    last_update=now_text,
                    note=note or "",
                    con_id=resolved_con_id,
                    order_ref=resolved_order_ref,
                    account=record.account,
                )
            )
        return snapshot, tuple(updated_records)
