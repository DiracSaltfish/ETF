from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from india_calendar import nifty_roll_month


MONTH_CODES = {
    1: "F",
    2: "G",
    3: "H",
    4: "J",
    5: "K",
    6: "M",
    7: "N",
    8: "Q",
    9: "U",
    10: "V",
    11: "X",
    12: "Z",
}


@dataclass(frozen=True)
class NiftyContract:
    trade_day: date
    expiry_month: str
    local_symbol: str
    exchange: str = "CME"
    currency: str = "USD"
    multiplier: int = 2
    con_id: int | None = None
    resolved_at: datetime | None = None
    source: str = "calendar_rule"

    def validate(self) -> None:
        if len(self.expiry_month) != 6 or not self.expiry_month.isdigit():
            raise ValueError("NIFTY expiry_month 必须是 YYYYMM")
        if self.multiplier != 2:
            raise ValueError("NIFTY 合约乘数必须为 2")
        if not self.local_symbol.upper().startswith("NIFTY"):
            raise ValueError("NIFTY 合约 local_symbol 无效")


def _month_from_local_symbol(value: str) -> str | None:
    text = str(value).strip().upper()
    if not text.startswith("NIFTY") or len(text) < 8:
        return None
    code = text[5]
    year_text = text[6:]
    month = next((number for number, letter in MONTH_CODES.items() if letter == code), None)
    if month is None or not year_text.isdigit():
        return None
    year = 2000 + int(year_text[-2:])
    return f"{year:04d}{month:02d}"


def resolve_nifty_contract(
    trade_day: date,
    *,
    roll_weekday: int = 1,
    override: str | None = None,
    contract_details: dict[str, object] | None = None,
) -> NiftyContract:
    if override:
        text = str(override).strip().upper()
        expiry = text if len(text) == 6 and text.isdigit() else _month_from_local_symbol(text)
        if expiry is None:
            raise ValueError("NIFTY override 应为 YYYYMM 或 NIFTY 月份代码")
        source = "manual_override"
    else:
        year, month = nifty_roll_month(trade_day, roll_weekday)
        expiry = f"{year:04d}{month:02d}"
        source = "calendar_rule"
    year = int(expiry[:4])
    month = int(expiry[4:])
    local_symbol = f"NIFTY{MONTH_CODES[month]}{year % 100:02d}"
    details = contract_details or {}
    if details.get("localSymbol"):
        local_symbol = str(details["localSymbol"])
    contract = NiftyContract(
        trade_day=trade_day,
        expiry_month=expiry,
        local_symbol=local_symbol,
        exchange=str(details.get("exchange") or "CME"),
        currency=str(details.get("currency") or "USD"),
        multiplier=int(details.get("multiplier") or 2),
        con_id=int(details["conId"]) if details.get("conId") else None,
        resolved_at=datetime.now(),
        source=source if not details else "tws_qualified",
    )
    if details.get("lastTradeDateOrContractMonth"):
        returned_expiry = str(details["lastTradeDateOrContractMonth"])[:6]
        if returned_expiry != expiry:
            raise ValueError(f"TWS 返回 NIFTY 合约月 {returned_expiry} 与计划 {expiry} 不一致")
    contract.validate()
    return contract
