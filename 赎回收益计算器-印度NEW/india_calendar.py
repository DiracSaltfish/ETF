from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


CHINA_TZ = ZoneInfo("Asia/Shanghai")
NEW_YORK_TZ = ZoneInfo("America/New_York")


class CalendarCoverageError(ValueError):
    """Raised when a calculation would use an unverified exchange calendar year."""


def _date_range(start: date, end: date) -> frozenset[date]:
    result: set[date] = set()
    current = start
    while current <= end:
        result.add(current)
        current += timedelta(days=1)
    return frozenset(result)


# Shenzhen Stock Exchange annual holiday notices. Weekend dates are included for
# auditability even though is_trading_day() rejects weekends first.
OFFICIAL_SZSE_HOLIDAYS_BY_YEAR: dict[int, frozenset[date]] = {
    2025: frozenset({date(2025, 1, 1)})
    | _date_range(date(2025, 1, 28), date(2025, 2, 4))
    | _date_range(date(2025, 4, 4), date(2025, 4, 6))
    | _date_range(date(2025, 5, 1), date(2025, 5, 5))
    | _date_range(date(2025, 5, 31), date(2025, 6, 2))
    | _date_range(date(2025, 10, 1), date(2025, 10, 8)),
    2026: _date_range(date(2026, 1, 1), date(2026, 1, 3))
    | _date_range(date(2026, 2, 15), date(2026, 2, 23))
    | _date_range(date(2026, 4, 4), date(2026, 4, 6))
    | _date_range(date(2026, 5, 1), date(2026, 5, 5))
    | _date_range(date(2026, 6, 19), date(2026, 6, 21))
    | _date_range(date(2026, 9, 25), date(2026, 9, 27))
    | _date_range(date(2026, 10, 1), date(2026, 10, 7)),
}
OFFICIAL_CALENDAR_YEARS = frozenset(OFFICIAL_SZSE_HOLIDAYS_BY_YEAR)
OFFICIAL_SZSE_HOLIDAYS = frozenset().union(*OFFICIAL_SZSE_HOLIDAYS_BY_YEAR.values())


@dataclass(frozen=True)
class TradingCalendar:
    holidays: frozenset[date] = OFFICIAL_SZSE_HOLIDAYS
    covered_years: frozenset[int] = OFFICIAL_CALENDAR_YEARS
    fund_closed_days: frozenset[date] = frozenset()
    strict: bool = True

    @classmethod
    def official(
        cls,
        *,
        extra_holidays: object = (),
        fund_closed_days: object = (),
        covered_years: object = (),
    ) -> "TradingCalendar":
        requested = parse_years(covered_years) or OFFICIAL_CALENDAR_YEARS
        missing = requested - OFFICIAL_CALENDAR_YEARS
        if missing:
            years = ", ".join(str(item) for item in sorted(missing))
            raise CalendarCoverageError(f"缺少 {years} 年深交所官方交易日历，已阻止计算")
        holidays = frozenset().union(*(OFFICIAL_SZSE_HOLIDAYS_BY_YEAR[year] for year in requested))
        holidays |= parse_holidays(extra_holidays)
        return cls(
            holidays=holidays,
            covered_years=requested,
            fund_closed_days=parse_holidays(fund_closed_days),
            strict=True,
        )

    def ensure_covered(self, day: date) -> None:
        if self.strict and day.year not in self.covered_years:
            raise CalendarCoverageError(f"缺少 {day.year} 年深交所官方交易日历，已阻止计算")

    def is_trading_day(self, day: date) -> bool:
        self.ensure_covered(day)
        return day.weekday() < 5 and day not in self.holidays and day not in self.fund_closed_days

    def offset(self, day: date, count: int) -> date:
        """Move by trading days, excluding the starting day."""
        current = day
        step = 1 if count >= 0 else -1
        remaining = abs(count)
        while remaining:
            current += timedelta(days=step)
            if self.is_trading_day(current):
                remaining -= 1
        return current

    def next_trading_day(self, day: date) -> date:
        return self.offset(day, 1)

    def previous_sessions(self, day: date, count: int) -> tuple[date, ...]:
        if count < 0:
            raise ValueError("count 不能小于 0")
        sessions = [self.offset(day, -index) for index in range(count, 0, -1)]
        return tuple(sessions)

    def eligible_day(self, buy_day: date, holding_days: int = 3) -> date:
        if holding_days < 1:
            raise ValueError("holding_days 必须大于 0")
        return self.offset(buy_day, holding_days)

    def settlement_days(self, redeem_day: date, statement_days: int, available_days: int) -> tuple[date, date]:
        return (
            self.offset(redeem_day, statement_days),
            self.offset(redeem_day, available_days),
        )


def last_weekday_of_month(year: int, month: int, weekday: int) -> date:
    if not 0 <= weekday <= 6:
        raise ValueError("weekday 必须位于 0 到 6")
    if month == 12:
        first_next = date(year + 1, 1, 1)
    else:
        first_next = date(year, month + 1, 1)
    current = first_next - timedelta(days=1)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def add_months(year: int, month: int, count: int) -> tuple[int, int]:
    absolute = year * 12 + month - 1 + count
    return absolute // 12, absolute % 12 + 1


def nifty_roll_month(trade_day: date, roll_weekday: int = 1) -> tuple[int, int]:
    """Return the default NIFTY expiry month for a China trading day.

    The agreed operational rule is to roll from the last Tuesday itself:
    2026-07-27 still uses 202607, while 2026-07-28 uses 202608.
    """
    roll_day = last_weekday_of_month(trade_day.year, trade_day.month, roll_weekday)
    if trade_day >= roll_day:
        return add_months(trade_day.year, trade_day.month, 1)
    return trade_day.year, trade_day.month


def parse_holidays(values: object) -> frozenset[date]:
    if not isinstance(values, (list, tuple, set)):
        return frozenset()
    result: set[date] = set()
    for value in values:
        try:
            result.add(date.fromisoformat(str(value)))
        except ValueError:
            continue
    return frozenset(result)


def parse_years(values: object) -> frozenset[int]:
    if not isinstance(values, (list, tuple, set, frozenset)):
        return frozenset()
    result: set[int] = set()
    for value in values:
        try:
            year = int(value)
        except (TypeError, ValueError):
            continue
        if 2000 <= year <= 2100:
            result.add(year)
    return frozenset(result)


def parse_local_time(value: str) -> time:
    text = str(value).strip()
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(text, pattern).time()
        except ValueError:
            continue
    raise ValueError(f"时间格式应为 HH:MM 或 HH:MM:SS：{value}")


def zoned_datetime(day: date, local_time: str | time, zone: ZoneInfo = NEW_YORK_TZ) -> datetime:
    parsed = parse_local_time(local_time) if isinstance(local_time, str) else local_time
    return datetime.combine(day, parsed, zone)


def beijing_display(et_datetime: datetime) -> str:
    current = et_datetime
    if current.tzinfo is None:
        current = current.replace(tzinfo=NEW_YORK_TZ)
    return current.astimezone(CHINA_TZ).strftime("%Y-%m-%d %H:%M:%S %Z")
