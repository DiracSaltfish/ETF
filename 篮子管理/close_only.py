from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime

from basket_models import BasketDocument, PortfolioPosition, normalize_symbol


EPSILON = 1e-9
TERMINAL_ORDER_STATUSES = {
    "Filled",
    "Cancelled",
    "ApiCancelled",
    "Inactive",
}


@dataclass(frozen=True)
class ActiveOrderSnapshot:
    account: str
    con_id: int
    symbol: str
    action: str
    total_quantity: float
    filled: float
    remaining: float
    status: str
    order_id: int = 0
    perm_id: int = 0
    order_ref: str = ""

    @property
    def is_active(self) -> bool:
        # PendingCancel remains active until IB confirms cancellation.  Some
        # callbacks report remaining=0 before the terminal status arrives, so
        # status is the primary gate.
        return self.status not in TERMINAL_ORDER_STATUSES


@dataclass(frozen=True)
class CloseCampaignEntry:
    symbol: str
    con_id: int
    initial_position: float
    reference_price: float
    role: str


@dataclass(frozen=True)
class CloseCampaignBasis:
    campaign_id: str
    account: str
    base_symbol: str
    created_at: str
    entries: tuple[CloseCampaignEntry, ...]
    scope_symbols: tuple[str, ...] = ()

    @property
    def entry_map(self) -> dict[str, CloseCampaignEntry]:
        return {entry.symbol: entry for entry in self.entries}


@dataclass(frozen=True)
class CloseOnlyLine:
    account: str
    symbol: str
    con_id: int
    role: str
    action: str
    current_position: float
    quantity: int
    projected_position: float
    market_price: float
    market_value: float

    @property
    def signed_order_quantity(self) -> int:
        return self.quantity if self.action == "BUY" else -self.quantity

    @property
    def closes_only(self) -> bool:
        if self.action == "BUY":
            return self.current_position < -EPSILON and self.projected_position <= EPSILON
        return self.current_position > EPSILON and self.projected_position >= -EPSILON


@dataclass(frozen=True)
class CloseOnlyPlan:
    plan_id: str
    created_at: str
    account: str
    base_symbol: str
    tranche_percent: int
    basis: CloseCampaignBasis
    lines: tuple[CloseOnlyLine, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    active_orders: tuple[ActiveOrderSnapshot, ...]
    position_signature: str
    approval_fingerprint: str

    @property
    def can_execute(self) -> bool:
        return bool(self.lines) and not self.blockers and not any(
            order.is_active for order in self.active_orders
        )

    @property
    def component_lines(self) -> tuple[CloseOnlyLine, ...]:
        return tuple(line for line in self.lines if line.role == "COMPONENT")

    @property
    def base_lines(self) -> tuple[CloseOnlyLine, ...]:
        return tuple(line for line in self.lines if line.role == "BASE")

    @property
    def total_component_buy_qty(self) -> int:
        return sum(
            line.quantity
            for line in self.component_lines
            if line.action == "BUY"
        )

    @property
    def total_base_sell_qty(self) -> int:
        return sum(
            line.quantity
            for line in self.base_lines
            if line.action == "SELL"
        )


def basket_component_symbols(
    basket: BasketDocument,
    *,
    base_symbol: str = "XOP",
) -> tuple[str, ...]:
    base = normalize_symbol(base_symbol)
    return tuple(sorted({item.symbol for item in basket.rows if item.symbol != base}))


def _whole_share(value: float) -> bool:
    return abs(value - round(value)) < EPSILON


def _reference_price(position: PortfolioPosition) -> float:
    if position.market_price > 0:
        return float(position.market_price)
    if position.quantity and position.market_value:
        price = abs(float(position.market_value) / float(position.quantity))
        if price > 0:
            return price
    return max(float(position.avg_cost), 0.0)


def create_campaign_basis(
    *,
    account: str,
    base_symbol: str,
    component_symbols: tuple[str, ...],
    position_map: dict[str, PortfolioPosition],
    now: datetime | None = None,
) -> CloseCampaignBasis:
    base = normalize_symbol(base_symbol)
    entries: list[CloseCampaignEntry] = []
    for symbol in component_symbols + (base,):
        position = position_map.get(symbol)
        if position is None or abs(position.quantity) < EPSILON:
            continue
        entries.append(
            CloseCampaignEntry(
                symbol=symbol,
                con_id=int(position.con_id),
                initial_position=float(position.quantity),
                reference_price=_reference_price(position),
                role="BASE" if symbol == base else "COMPONENT",
            )
        )
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    return CloseCampaignBasis(
        campaign_id=f"UW-{uuid.uuid4().hex[:12].upper()}",
        account=account,
        base_symbol=base,
        created_at=timestamp,
        entries=tuple(sorted(entries, key=lambda item: (item.role == "BASE", item.symbol))),
        scope_symbols=tuple(sorted(set(component_symbols) | {base})),
    )


def _close_quantity(position: float, percent: int) -> int:
    absolute = abs(position)
    if absolute < EPSILON:
        return 0
    if percent >= 100:
        return int(round(absolute))
    quantity = int(math.floor(absolute * percent / 100.0 + 0.5))
    return min(int(round(absolute)), max(1, quantity))


def _round_nonnegative_share(value: float) -> int:
    return int(math.floor(max(float(value), 0.0) + 0.5))


def _position_signature(
    account: str,
    positions: tuple[PortfolioPosition, ...],
    scoped_orders: tuple[ActiveOrderSnapshot, ...],
) -> str:
    payload = {
        "account": account,
        "positions": sorted(
            (
                int(item.con_id),
                item.symbol,
                round(float(item.quantity), 8),
            )
            for item in positions
            if item.account == account
        ),
        "active_orders": sorted(
            (
                item.order_id,
                item.perm_id,
                item.con_id,
                item.symbol,
                item.action,
                round(float(item.remaining), 8),
                item.status,
            )
            for item in scoped_orders
            if item.is_active
        ),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _approval_fingerprint(
    account: str,
    basis: CloseCampaignBasis,
    lines: tuple[CloseOnlyLine, ...],
    position_signature: str,
) -> str:
    payload = {
        "account": account,
        "basis": asdict(basis),
        "position_signature": position_signature,
        "lines": [
            (
                line.con_id,
                line.symbol,
                line.action,
                line.quantity,
                round(line.current_position, 8),
                round(line.projected_position, 8),
            )
            for line in lines
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def build_close_only_plan(
    basket: BasketDocument,
    positions: tuple[PortfolioPosition, ...],
    *,
    account: str,
    active_orders: tuple[ActiveOrderSnapshot, ...] = (),
    tranche_percent: int = 25,
    base_symbol: str = "XOP",
    campaign_basis: CloseCampaignBasis | None = None,
    now: datetime | None = None,
) -> CloseOnlyPlan:
    if tranche_percent not in {5, 10, 25, 50, 100}:
        raise ValueError("平仓批次百分比仅支持 5/10/25/50/100")
    account = str(account or "").strip()
    base = normalize_symbol(base_symbol)
    components = basket_component_symbols(basket, base_symbol=base)
    scope = set(components) | {base}
    blockers: list[str] = []
    warnings: list[str] = []

    if not account:
        blockers.append("必须明确选择 IBKR 账户")

    grouped: dict[str, list[PortfolioPosition]] = {}
    for position in positions:
        if position.account != account or position.symbol not in scope:
            continue
        grouped.setdefault(position.symbol, []).append(position)

    position_map: dict[str, PortfolioPosition] = {}
    for symbol, rows in grouped.items():
        nonzero = [row for row in rows if abs(row.quantity) >= EPSILON]
        con_ids = {int(row.con_id) for row in nonzero}
        if len(nonzero) > 1 or len(con_ids) > 1:
            blockers.append(f"{symbol} 在账户中存在多条合约/持仓，禁止按代码聚合平仓")
            continue
        if nonzero:
            position_map[symbol] = nonzero[0]

    scoped_orders = tuple(
        order
        for order in active_orders
        if order.account == account
        and (order.symbol in scope or (order.con_id and order.con_id in {p.con_id for p in position_map.values()}))
        and order.is_active
    )
    for order in scoped_orders:
        blockers.append(
            f"存在活动订单 {order.symbol} {order.action} remaining={order.remaining:g} "
            f"status={order.status} orderId={order.order_id}"
        )

    for symbol, position in sorted(position_map.items()):
        if position.sec_type != "STK" or position.currency != "USD":
            blockers.append(
                f"{symbol} 合约类型异常: {position.sec_type}/{position.currency}"
            )
        if int(position.con_id) <= 0:
            blockers.append(f"{symbol} 缺少有效 conId")
        if not _whole_share(position.quantity):
            blockers.append(f"{symbol} 为碎股持仓 {position.quantity:g}，当前版本禁止自动取整")
        if symbol == base and position.quantity < -EPSILON:
            blockers.append(f"{base} 已是空头 {position.quantity:g}，Close Only 禁止继续卖出")
        if symbol != base and position.quantity > EPSILON:
            blockers.append(f"成分券 {symbol} 已是多头 {position.quantity:g}，Close Only 禁止继续买入")

    if campaign_basis is None:
        campaign_basis = create_campaign_basis(
            account=account,
            base_symbol=base,
            component_symbols=components,
            position_map=position_map,
            now=now,
        )
    elif campaign_basis.account != account or campaign_basis.base_symbol != base:
        blockers.append("未完成的平仓会话与当前账户或基准标的不一致")

    basis_map = campaign_basis.entry_map
    basis_symbols = [entry.symbol for entry in campaign_basis.entries]
    if len(basis_symbols) != len(set(basis_symbols)):
        blockers.append("平仓会话包含重复标的，禁止执行")
    if set(campaign_basis.scope_symbols) != scope:
        blockers.append("篮子成分范围与原始平仓会话不一致，可能曾原地修改篮子文件")
    for entry in campaign_basis.entries:
        if entry.symbol not in scope:
            blockers.append(f"平仓会话包含篮子范围外标的 {entry.symbol}")
        if entry.con_id <= 0:
            blockers.append(f"平仓会话 {entry.symbol} conId 无效")
        expected_role = "BASE" if entry.symbol == base else "COMPONENT"
        if entry.role != expected_role:
            blockers.append(f"平仓会话 {entry.symbol} 角色异常: {entry.role}")
        if entry.reference_price <= 0:
            blockers.append(f"平仓会话 {entry.symbol} 缺少有效固定参考价")
        if entry.role == "BASE" and entry.initial_position <= 0:
            blockers.append(f"平仓会话 {entry.symbol} 初始仓位不是多头")
        if entry.role == "COMPONENT" and entry.initial_position >= 0:
            blockers.append(f"平仓会话 {entry.symbol} 初始仓位不是空头")
    for symbol, position in position_map.items():
        entry = basis_map.get(symbol)
        if entry is None:
            blockers.append(f"{symbol} 不在原始平仓会话中，可能存在外部新增仓位")
            continue
        if entry.con_id != int(position.con_id):
            blockers.append(
                f"{symbol} conId 已变化: 会话 {entry.con_id} / 当前 {position.con_id}"
            )
        if abs(position.quantity) > abs(entry.initial_position) + EPSILON:
            blockers.append(
                f"{symbol} 当前仓位 {position.quantity:g} 超过会话初始仓位 "
                f"{entry.initial_position:g}，可能发生了外部加仓"
            )

    component_lines: list[CloseOnlyLine] = []
    projected_component_positions: dict[str, float] = {}
    for symbol in components:
        position = position_map.get(symbol)
        current = float(position.quantity) if position else 0.0
        if current >= -EPSILON:
            projected_component_positions[symbol] = current
            continue
        quantity = _close_quantity(current, tranche_percent)
        projected = min(current + quantity, 0.0)
        projected_component_positions[symbol] = projected
        component_lines.append(
            CloseOnlyLine(
                account=account,
                symbol=symbol,
                con_id=int(position.con_id),
                role="COMPONENT",
                action="BUY",
                current_position=current,
                quantity=quantity,
                projected_position=projected,
                market_price=float(position.market_price),
                market_value=float(position.market_value),
            )
        )

    initial_component_notional = sum(
        abs(entry.initial_position) * entry.reference_price
        for entry in campaign_basis.entries
        if entry.role == "COMPONENT"
    )
    projected_component_notional = sum(
        abs(projected_component_positions.get(entry.symbol, 0.0)) * entry.reference_price
        for entry in campaign_basis.entries
        if entry.role == "COMPONENT"
    )
    initial_base_entry = next(
        (entry for entry in campaign_basis.entries if entry.role == "BASE"),
        None,
    )
    base_position = position_map.get(base)
    current_base = float(base_position.quantity) if base_position else 0.0
    desired_base_after = 0
    if initial_base_entry and initial_component_notional > EPSILON:
        desired_base_after = _round_nonnegative_share(
            max(initial_base_entry.initial_position, 0.0)
            * projected_component_notional
            / initial_component_notional
        )
    if current_base + EPSILON < desired_base_after:
        warnings.append(
            f"当前 {base} 多头少于成分券剩余敞口对应值；本批不会买入 {base}，"
            "只会继续降低成分券空头"
        )
    base_sell_quantity = max(0, int(round(current_base)) - desired_base_after)
    base_sell_quantity = min(base_sell_quantity, max(int(round(current_base)), 0))

    base_lines: list[CloseOnlyLine] = []
    if base_sell_quantity > 0 and base_position is not None:
        projected = max(current_base - base_sell_quantity, 0.0)
        base_lines.append(
            CloseOnlyLine(
                account=account,
                symbol=base,
                con_id=int(base_position.con_id),
                role="BASE",
                action="SELL",
                current_position=current_base,
                quantity=base_sell_quantity,
                projected_position=projected,
                market_price=float(base_position.market_price),
                market_value=float(base_position.market_value),
            )
        )

    lines = tuple(component_lines + base_lines)
    for line in lines:
        if line.quantity <= 0 or not line.closes_only:
            blockers.append(
                f"{line.symbol} 平仓数量校验失败: {line.current_position:g} -> "
                f"{line.projected_position:g}"
            )

    if not lines and not blockers:
        warnings.append("策略范围内仓位已经全部为 0")
    if tranche_percent < 100:
        warnings.append(
            f"当前为 {tranche_percent}% 分批释放；每批完成并确认无活动订单后再生成下一批"
        )

    signature_positions = tuple(
        position
        for position in positions
        if position.account == account and position.symbol in scope
    )
    signature = _position_signature(account, signature_positions, scoped_orders)
    approval = _approval_fingerprint(account, campaign_basis, lines, signature)
    timestamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    return CloseOnlyPlan(
        plan_id=f"PLAN-{uuid.uuid4().hex[:10].upper()}",
        created_at=timestamp,
        account=account,
        base_symbol=base,
        tranche_percent=tranche_percent,
        basis=campaign_basis,
        lines=lines,
        blockers=tuple(dict.fromkeys(blockers)),
        warnings=tuple(dict.fromkeys(warnings)),
        active_orders=scoped_orders,
        position_signature=signature,
        approval_fingerprint=approval,
    )


def plans_match_for_execution(approved: CloseOnlyPlan, fresh: CloseOnlyPlan) -> bool:
    def execution_fields(plan: CloseOnlyPlan):
        return tuple(
            (
                line.account,
                line.symbol,
                line.con_id,
                line.role,
                line.action,
                round(line.current_position, 8),
                line.quantity,
                round(line.projected_position, 8),
            )
            for line in plan.lines
        )

    return (
        approved.account == fresh.account
        and approved.base_symbol == fresh.base_symbol
        and approved.tranche_percent == fresh.tranche_percent
        and approved.basis.campaign_id == fresh.basis.campaign_id
        and approved.position_signature == fresh.position_signature
        and approved.approval_fingerprint == fresh.approval_fingerprint
        and execution_fields(approved) == execution_fields(fresh)
        and not fresh.blockers
    )


def compute_base_sell_due(
    basis: CloseCampaignBasis,
    positions: tuple[PortfolioPosition, ...],
) -> int:
    grouped: dict[str, list[PortfolioPosition]] = {}
    for item in positions:
        if item.account == basis.account and item.symbol in basis.entry_map and abs(item.quantity) > EPSILON:
            grouped.setdefault(item.symbol, []).append(item)
    position_map: dict[str, PortfolioPosition] = {}
    for entry in basis.entries:
        rows = grouped.get(entry.symbol, [])
        if len(rows) > 1:
            raise ValueError(f"{entry.symbol} 成交后出现重复合约持仓，停止同步基准腿")
        if not rows:
            continue
        position = rows[0]
        if int(position.con_id) != entry.con_id:
            raise ValueError(f"{entry.symbol} 成交后 conId 变化，停止同步基准腿")
        if abs(position.quantity) > abs(entry.initial_position) + EPSILON:
            raise ValueError(f"{entry.symbol} 成交后仓位超过会话初始值，停止同步基准腿")
        if entry.role == "COMPONENT" and position.quantity > EPSILON:
            raise ValueError(f"{entry.symbol} 成交后穿越为多头，停止同步基准腿")
        if entry.role == "BASE" and position.quantity < -EPSILON:
            raise ValueError(f"{entry.symbol} 成交后穿越为空头，停止同步基准腿")
        position_map[entry.symbol] = position
    initial_component_notional = sum(
        abs(entry.initial_position) * entry.reference_price
        for entry in basis.entries
        if entry.role == "COMPONENT"
    )
    current_component_notional = sum(
        abs(float(position_map.get(entry.symbol).quantity)) * entry.reference_price
        for entry in basis.entries
        if entry.role == "COMPONENT" and position_map.get(entry.symbol) is not None
    )
    base_entry = next((entry for entry in basis.entries if entry.role == "BASE"), None)
    if base_entry is None:
        return 0
    base_position = position_map.get(basis.base_symbol)
    current_base = max(float(base_position.quantity), 0.0) if base_position else 0.0
    desired_base = 0
    if initial_component_notional > EPSILON:
        desired_base = _round_nonnegative_share(
            max(base_entry.initial_position, 0.0)
            * current_component_notional
            / initial_component_notional
        )
    return min(
        max(int(round(current_base)) - desired_base, 0),
        int(round(current_base)),
    )


def campaign_basis_to_dict(basis: CloseCampaignBasis) -> dict[str, object]:
    return asdict(basis)


def campaign_basis_from_dict(payload: dict[str, object]) -> CloseCampaignBasis:
    raw_entries = payload.get("entries") or []
    entries = tuple(
        CloseCampaignEntry(
            symbol=normalize_symbol(item.get("symbol")),
            con_id=int(item.get("con_id") or 0),
            initial_position=float(item.get("initial_position") or 0.0),
            reference_price=float(item.get("reference_price") or 0.0),
            role=str(item.get("role") or "COMPONENT"),
        )
        for item in raw_entries
        if isinstance(item, dict)
    )
    return CloseCampaignBasis(
        campaign_id=str(payload.get("campaign_id") or ""),
        account=str(payload.get("account") or ""),
        base_symbol=normalize_symbol(payload.get("base_symbol") or "XOP"),
        created_at=str(payload.get("created_at") or ""),
        entries=entries,
        scope_symbols=tuple(
            sorted(
                {
                    normalize_symbol(symbol)
                    for symbol in (payload.get("scope_symbols") or [entry.symbol for entry in entries])
                    if normalize_symbol(symbol)
                }
            )
        ),
    )
