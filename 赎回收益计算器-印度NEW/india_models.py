from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Literal


Action = Literal["BUY", "SELL", "REDEEM"]


@dataclass(frozen=True)
class IndiaTrade:
    account: str
    row_number: int
    trade_day: date
    action: Action
    qty: int
    price: Decimal = Decimal("0")
    amount: Decimal = Decimal("0")
    contract_no: str = ""
    code: str = "164824"
    name: str = ""
    trade_dt: datetime | None = None

    @property
    def event_dt(self) -> datetime:
        return self.trade_dt or datetime.combine(self.trade_day, datetime.min.time())


@dataclass(frozen=True)
class PositionSnapshot:
    day: date
    account: str
    total_qty: int
    available_qty: int
    source_path: str


@dataclass(frozen=True)
class RedemptionEvent:
    event_id: str
    account: str
    redeem_day: date
    qty: int
    source: str = "manual"
    contract_no: str = ""
    gross_amount: Decimal | None = None
    fee_amount: Decimal | None = None
    net_amount: Decimal | None = None
    nav_per_share: Decimal | None = None
    event_dt: datetime | None = None
    statement_day: date | None = None
    raw_reference: str = ""

    def __post_init__(self) -> None:
        if self.qty <= 0:
            raise ValueError("赎回数量必须大于 0")

    @property
    def effective_dt(self) -> datetime:
        return self.event_dt or datetime.combine(self.redeem_day, datetime.max.time())


@dataclass
class IndiaLot:
    lot_id: str
    account: str
    buy_day: date
    eligible_day: date
    qty: int
    remaining_qty: int
    cost_per_share: Decimal
    source_row: int

    @property
    def remaining_cost(self) -> Decimal:
        return self.cost_per_share * Decimal(self.remaining_qty)


@dataclass(frozen=True)
class LotMatch:
    lot_id: str
    buy_day: date
    qty: int
    cost: Decimal


@dataclass(frozen=True)
class IndiaSettlement:
    expected_statement_day: date
    expected_available_day: date
    actual_statement_day: date | None = None
    actual_available_day: date | None = None
    gross_amount: Decimal | None = None
    fee_amount: Decimal | None = None
    net_amount: Decimal | None = None
    nav_per_share: Decimal | None = None
    amount_source: str = "unknown"
    available_day_source: str = "unknown"

    @property
    def status(self) -> str:
        if self.net_amount is None:
            return "waiting_statement"
        if self.actual_available_day is not None:
            return "available"
        if self.actual_statement_day is not None:
            return "credited_pending_use"
        return "estimated"


@dataclass
class HedgeSummary:
    nifty_open_qty: int = 0
    nifty_close_qty: int = 0
    nifty_open_avg: Decimal | None = None
    nifty_close_avg: Decimal | None = None
    nifty_pnl_usd: Decimal = Decimal("0")
    inda_open_qty: int = 0
    inda_close_qty: int = 0
    inda_open_avg: Decimal | None = None
    inda_close_avg: Decimal | None = None
    inda_pnl_usd: Decimal = Decimal("0")
    commissions_usd: Decimal = Decimal("0")
    borrow_fee_usd: Decimal = Decimal("0")
    fx_rate: Decimal = Decimal("0")
    pnl_cny: Decimal = Decimal("0")
    warnings: list[str] = field(default_factory=list)

    @property
    def total_pnl_usd(self) -> Decimal:
        return self.nifty_pnl_usd + self.inda_pnl_usd - self.commissions_usd - self.borrow_fee_usd


@dataclass
class IndiaBasket:
    basket_id: str
    account: str
    redeem_day: date
    contract_no: str
    sequence: int
    redeem_qty: int
    domestic_cost: Decimal
    domestic_matches: tuple[LotMatch, ...] = ()
    inventory_shortfall: int = 0
    settlement: IndiaSettlement | None = None
    hedge: HedgeSummary = field(default_factory=HedgeSummary)
    data_quality: str = "complete"
    redemption_status: str = "confirmed"
    settlement_status: str = "waiting_statement"
    hedge_status: str = "not_started"
    warnings: list[str] = field(default_factory=list)

    @property
    def is_standard(self) -> bool:
        return self.redeem_qty == 270000

    @property
    def domestic_net_amount(self) -> Decimal | None:
        return self.settlement.net_amount if self.settlement else None

    @property
    def domestic_pnl(self) -> Decimal | None:
        amount = self.domestic_net_amount
        return None if amount is None else amount - self.domestic_cost

    @property
    def total_pnl_cny(self) -> Decimal | None:
        domestic = self.domestic_pnl
        if domestic is None:
            return None
        return domestic + self.hedge.pnl_cny

    @property
    def nifty_target(self) -> int:
        return 1 if self.is_standard else 0

    @property
    def inda_target(self) -> int:
        return 970 if self.is_standard else 0


@dataclass(frozen=True)
class IbFill:
    fill_id: str
    symbol: str
    asset_class: str
    dt: datetime
    qty: int
    price: Decimal
    commission: Decimal = Decimal("0")
    currency: str = "USD"
    order_ref: str = ""

    @property
    def side(self) -> str:
        return "BUY" if self.qty > 0 else "SELL"

    @property
    def abs_qty(self) -> int:
        return abs(self.qty)


@dataclass(frozen=True)
class IndiaOrderSpec:
    order_ref: str
    sequence: int
    trade_day: date
    symbol: str
    action: str
    quantity: int
    trigger_dt: datetime
    purpose: str
    contract_month: str = ""
    live_allowed: bool = False

    @property
    def trigger_time_et(self) -> str:
        return self.trigger_dt.strftime("%Y-%m-%d %H:%M:%S %Z")


@dataclass(frozen=True)
class StatementImportIssue:
    row_number: int
    message: str
    raw: str = ""


@dataclass(frozen=True)
class StatementImportResult:
    events: tuple[RedemptionEvent, ...]
    issues: tuple[StatementImportIssue, ...] = ()


@dataclass(frozen=True)
class IndiaCalculation:
    baskets: tuple[IndiaBasket, ...]
    trades: tuple[IndiaTrade, ...]
    redemptions: tuple[RedemptionEvent, ...]
    account_snapshots: dict[str, dict[str, object]]
    warnings: tuple[str, ...] = ()

    @property
    def standard_basket_count(self) -> int:
        return sum(item.nifty_target for item in self.baskets)

    @property
    def total_pnl_cny(self) -> Decimal | None:
        if self.baskets and any(item.total_pnl_cny is None for item in self.baskets):
            return None
        return sum(
            (item.total_pnl_cny or Decimal("0") for item in self.baskets),
            Decimal("0"),
        )
