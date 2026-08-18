from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


def normalize_symbol(value: object) -> str:
    text = str(value or "").strip().upper()
    if text.endswith(".0"):
        text = text[:-2]
    return text


def normalize_action(value: object, *, default: str = "SELL") -> str:
    text = str(value or "").strip().upper()
    buy_aliases = {"BUY", "B", "BOT", "买", "买入"}
    sell_aliases = {"SELL", "S", "SLD", "SHORT", "卖", "卖出", "卖空"}
    if text in buy_aliases:
        return "BUY"
    if text in sell_aliases:
        return "SELL"
    return default


@dataclass(frozen=True)
class ConnectionSettings:
    host: str
    port: int
    client_id: int
    account: str = ""


@dataclass(frozen=True)
class BasketItem:
    symbol: str
    action: str
    quantity: int
    name: str = ""
    source_sheet: str = ""
    source_row: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))
        object.__setattr__(self, "action", normalize_action(self.action))
        object.__setattr__(self, "quantity", abs(int(self.quantity)))

    @property
    def signed_target(self) -> int:
        return self.quantity if self.action == "BUY" else -self.quantity


@dataclass(frozen=True)
class BasketDocument:
    path: Path
    name: str
    rows: tuple[BasketItem, ...]
    metadata: dict[str, str] = field(default_factory=dict)

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def buy_rows(self) -> tuple[BasketItem, ...]:
        return tuple(item for item in self.rows if item.action == "BUY")

    @property
    def sell_rows(self) -> tuple[BasketItem, ...]:
        return tuple(item for item in self.rows if item.action == "SELL")

    @property
    def total_buy_qty(self) -> int:
        return sum(item.quantity for item in self.buy_rows)

    @property
    def total_sell_qty(self) -> int:
        return sum(item.quantity for item in self.sell_rows)


@dataclass(frozen=True)
class PortfolioPosition:
    account: str
    symbol: str
    local_symbol: str
    sec_type: str
    exchange: str
    currency: str
    quantity: float
    avg_cost: float
    market_price: float
    market_value: float
    unrealized_pnl: float
    realized_pnl: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))


@dataclass(frozen=True)
class SymbolMarketState:
    symbol: str
    market_price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    last: float = 0.0
    close: float = 0.0
    shortable_shares: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", normalize_symbol(self.symbol))


@dataclass(frozen=True)
class ReconciliationRow:
    item: BasketItem
    current_position: float
    target_position: float
    delta_to_target: float
    long_inventory: float
    shortable_shares: float | None
    execution_capacity: float
    execution_shortfall: float
    market_price: float
    market_value: float
    account: str
    target_status: str
    sell_status: str
    note: str

    @property
    def target_matched(self) -> bool:
        return abs(self.delta_to_target) < 1e-9

    @property
    def sell_ready(self) -> bool:
        return self.item.action != "SELL" or self.execution_shortfall <= 1e-9


@dataclass(frozen=True)
class ConnectionSnapshot:
    host: str
    port: int
    client_id: int
    managed_accounts: tuple[str, ...]
    active_account: str
    server_version: int
    server_time: str


@dataclass(frozen=True)
class SubmittedOrder:
    symbol: str
    action: str
    quantity: int
    order_type: str
    tif: str
    limit_price: float | None
    order_id: int
    perm_id: int
    status: str


@dataclass(frozen=True)
class OrderMonitorRecord:
    batch_id: str
    group_label: str
    submitted_at: str
    symbol: str
    action: str
    quantity: int
    order_type: str
    limit_price: float | None
    order_id: int
    perm_id: int
    status: str
    filled: float = 0.0
    remaining: float = 0.0
    avg_fill_price: float = 0.0
    last_update: str = ""
    note: str = ""
