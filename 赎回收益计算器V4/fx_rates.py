from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


SAFE_QUERY_URL = "https://www.safe.gov.cn/AppStructured/hlw/RMBQuery.do"
CFETS_REF_RATE_URL = "https://www.chinamoney.com.cn/ags/ms/cm-u-bk-fx/RefRateHis"
CSV_FIELDS = (
    "trade_date",
    "source",
    "pair",
    "display_name",
    "quote_time",
    "rate",
    "raw_rate",
    "quote_basis",
    "raw_basis",
    "fetched_at",
    "derived_from",
)
SAFE_SOURCE = "SAFE_CENTRAL_PARITY"
CFETS_SOURCE = "CFETS_REFERENCE_RATE"
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}
DISPLAY_PAIR = "USD/CNY"
# USD/CNY and HKD/CNY are both required to settle a 159605 PCF basket into
# CNY.  Keep DISPLAY_PAIR for the existing 159518 UI, while retaining the
# two official CFETS close rows in the same cache.
CFETS_SETTLEMENT_PAIRS = ("USD/CNY", "HKD/CNY")
DISPLAY_START_HOUR = 10
DISPLAY_END_HOUR = 18


@dataclass(frozen=True)
class _SafePairMeta:
    pair: str
    display_name: str
    raw_basis: str
    quote_basis: str
    divisor: Decimal


SAFE_PAIR_ORDER = (
    "USD/CNY",
    "EUR/CNY",
    "100JPY/CNY",
    "HKD/CNY",
    "GBP/CNY",
    "AUD/CNY",
    "NZD/CNY",
    "SGD/CNY",
    "CHF/CNY",
    "CAD/CNY",
    "CNY/MOP",
    "CNY/MYR",
    "CNY/RUB",
    "CNY/ZAR",
    "CNY/KRW",
    "CNY/AED",
    "CNY/SAR",
    "CNY/HUF",
    "CNY/PLN",
    "CNY/DKK",
    "CNY/SEK",
    "CNY/NOK",
    "CNY/TRY",
    "CNY/MXN",
    "CNY/THB",
)

SAFE_PAIR_MAP: dict[str, _SafePairMeta] = {
    "美元": _SafePairMeta("USD/CNY", "美元", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "欧元": _SafePairMeta("EUR/CNY", "欧元", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "日元": _SafePairMeta("100JPY/CNY", "日元", "100_foreign_to_cny", "100_foreign_to_cny", Decimal("1")),
    "港元": _SafePairMeta("HKD/CNY", "港元", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "英镑": _SafePairMeta("GBP/CNY", "英镑", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "澳元": _SafePairMeta("AUD/CNY", "澳元", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "新西兰元": _SafePairMeta("NZD/CNY", "新西兰元", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "新加坡元": _SafePairMeta("SGD/CNY", "新加坡元", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "瑞士法郎": _SafePairMeta("CHF/CNY", "瑞士法郎", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "加元": _SafePairMeta("CAD/CNY", "加元", "100_foreign_to_cny", "1_foreign_to_cny", Decimal("100")),
    "澳门元": _SafePairMeta("CNY/MOP", "澳门元", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "林吉特": _SafePairMeta("CNY/MYR", "林吉特", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "卢布": _SafePairMeta("CNY/RUB", "卢布", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "兰特": _SafePairMeta("CNY/ZAR", "兰特", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "韩元": _SafePairMeta("CNY/KRW", "韩元", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "迪拉姆": _SafePairMeta("CNY/AED", "迪拉姆", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "里亚尔": _SafePairMeta("CNY/SAR", "里亚尔", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "福林": _SafePairMeta("CNY/HUF", "福林", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "兹罗提": _SafePairMeta("CNY/PLN", "兹罗提", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "丹麦克朗": _SafePairMeta("CNY/DKK", "丹麦克朗", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "瑞典克朗": _SafePairMeta("CNY/SEK", "瑞典克朗", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "挪威克朗": _SafePairMeta("CNY/NOK", "挪威克朗", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "里拉": _SafePairMeta("CNY/TRY", "里拉", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "比索": _SafePairMeta("CNY/MXN", "比索", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
    "泰铢": _SafePairMeta("CNY/THB", "泰铢", "100_cny_to_foreign", "1_cny_to_foreign", Decimal("100")),
}


class FxRateError(RuntimeError):
    pass


class _HtmlTableParser(HTMLParser):
    def __init__(self, table_id: str) -> None:
        super().__init__()
        self.table_id = table_id
        self.in_table = False
        self.table_depth = 0
        self.in_row = False
        self.in_cell = False
        self.current_row: list[str] = []
        self.current_cell: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attr_map = {key: value or "" for key, value in attrs}
        if tag == "table" and attr_map.get("id") == self.table_id:
            self.in_table = True
            self.table_depth = 1
            return
        if not self.in_table:
            return
        if tag == "table":
            self.table_depth += 1
            return
        if tag == "tr":
            self.in_row = True
            self.current_row = []
            return
        if tag in {"td", "th"} and self.in_row:
            self.in_cell = True
            self.current_cell = []
            return
        if tag == "br" and self.in_cell:
            self.current_cell.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag in {"td", "th"} and self.in_cell:
            value = _clean_text("".join(self.current_cell))
            self.current_row.append(value)
            self.in_cell = False
            self.current_cell = []
            return
        if tag == "tr" and self.in_row:
            if any(cell for cell in self.current_row):
                self.rows.append(self.current_row)
            self.in_row = False
            self.current_row = []
            return
        if tag == "table":
            self.table_depth -= 1
            if self.table_depth <= 0:
                self.in_table = False
                self.table_depth = 0

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.current_cell.append(data)


def pair_sort_key(pair: str) -> tuple[int, str]:
    try:
        return SAFE_PAIR_ORDER.index(pair), pair
    except ValueError:
        return len(SAFE_PAIR_ORDER), pair


class FxRateStore:
    def __init__(self, csv_path: Path | str, fetch_bytes=None) -> None:
        self.csv_path = Path(csv_path).expanduser().resolve()
        self._fetch_bytes = fetch_bytes or self._http_request

    def load_day_records(self, trading_day: date) -> list[dict[str, str]]:
        if not self.csv_path.exists():
            return []
        day_text = trading_day.isoformat()
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            return [dict(row) for row in reader if str(row.get("trade_date") or "") == day_text]

    def _get_rate(
        self,
        trading_day: date,
        source: str,
        quote_time: str,
        pair: str = DISPLAY_PAIR,
    ) -> Decimal | None:
        for row in self.load_day_records(trading_day):
            if (
                row.get("source") == source
                and row.get("pair") == pair
                and row.get("quote_time") == quote_time
            ):
                try:
                    return Decimal(str(row.get("rate") or ""))
                except InvalidOperation:
                    return None
        return None

    def get_usd_cny_safe_mid(self, trading_day: date) -> Decimal | None:
        return self._get_rate(trading_day, SAFE_SOURCE, "CENTRAL")

    def get_usd_cny_cfets_close(self, trading_day: date) -> Decimal | None:
        return self._get_rate(trading_day, CFETS_SOURCE, "CLOSE")

    def get_cfets_close(self, trading_day: date, pair: str) -> Decimal | None:
        """Return one unit of ``pair`` in CNY at CFETS' published T-day close."""
        return self._get_rate(trading_day, CFETS_SOURCE, "CLOSE", pair=pair)

    def get_hkd_cny_cfets_close(self, trading_day: date) -> Decimal | None:
        return self.get_cfets_close(trading_day, "HKD/CNY")

    def get_usd_cny_cfets_hour(self, trading_day: date, hour: str) -> Decimal | None:
        return self._get_rate(trading_day, CFETS_SOURCE, hour)

    def refresh_cfets_date(self, trading_day: date) -> list[dict[str, str]]:
        """Refresh only CFETS records without coupling the request to SAFE data."""
        new_rows = self.fetch_cfets_records(trading_day)
        if new_rows:
            self._upsert_rows(trading_day, {CFETS_SOURCE}, new_rows)
        return self.load_day_records(trading_day)

    def ensure_trade_date(self, trading_day: date, force_refresh: bool = False) -> list[dict[str, str]]:
        records = self.load_day_records(trading_day)
        present_sources = {row["source"] for row in records}
        cfets_pairs_present = {
            str(row.get("pair") or "")
            for row in records
            if row.get("source") == CFETS_SOURCE and row.get("quote_time") == "CLOSE"
        }
        targets = {SAFE_SOURCE, CFETS_SOURCE} if force_refresh else {
            source
            for source in (SAFE_SOURCE, CFETS_SOURCE)
            if source not in present_sources
            or (source == CFETS_SOURCE and not set(CFETS_SETTLEMENT_PAIRS).issubset(cfets_pairs_present))
        }
        if not targets:
            return records

        new_rows: list[dict[str, str]] = []
        if SAFE_SOURCE in targets:
            new_rows.extend(self.fetch_safe_records(trading_day))
        if CFETS_SOURCE in targets:
            new_rows.extend(self.fetch_cfets_records(trading_day))
        self._upsert_rows(trading_day, targets, new_rows)
        return self.load_day_records(trading_day)

    def build_day_matrix(self, trading_day: date) -> tuple[list[str], list[dict[str, str]]]:
        rows = self.load_day_records(trading_day)
        safe_rows = {
            row["pair"]: row
            for row in rows
            if row["source"] == SAFE_SOURCE and row["quote_time"] == "CENTRAL" and row["pair"] == DISPLAY_PAIR
        }
        cfets_by_pair: dict[str, dict[str, str]] = {}
        close_by_pair: dict[str, dict[str, str]] = {}
        hour_columns: list[str] = []
        for row in rows:
            if row["source"] != CFETS_SOURCE:
                continue
            pair = row["pair"]
            if pair != DISPLAY_PAIR:
                continue
            if row["quote_time"] == "CLOSE":
                close_by_pair[pair] = row
                continue
            if not _is_display_hour(row["quote_time"]):
                continue
            cfets_by_pair.setdefault(pair, {})[row["quote_time"]] = row["rate"]
            if row["quote_time"] not in hour_columns:
                hour_columns.append(row["quote_time"])

        hour_columns.sort(key=_hour_sort_key)
        pairs = sorted(set(safe_rows) | set(cfets_by_pair) | set(close_by_pair), key=pair_sort_key)
        matrix: list[dict[str, str]] = []
        for pair in pairs:
            safe_row = safe_rows.get(pair)
            close_row = close_by_pair.get(pair)
            item = {
                "pair": pair,
                "display_name": safe_row["display_name"] if safe_row else pair,
                "safe_rate": safe_row["rate"] if safe_row else "",
                "safe_basis": safe_row["quote_basis"] if safe_row else "",
                "close_rate": close_row["rate"] if close_row else "",
                "close_time": close_row["derived_from"] if close_row else "",
            }
            for hour in hour_columns:
                item[hour] = cfets_by_pair.get(pair, {}).get(hour, "")
            matrix.append(item)
        return hour_columns, matrix

    def fetch_safe_records(self, trading_day: date) -> list[dict[str, str]]:
        payload = urlencode(
            {
                "startDate": trading_day.isoformat(),
                "endDate": trading_day.isoformat(),
                "queryYN": "true",
            }
        ).encode("utf-8")
        html = self._fetch_bytes(
            SAFE_QUERY_URL,
            data=payload,
            headers={**HTTP_HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        ).decode("utf-8", errors="ignore")
        parser = _HtmlTableParser("InfoTable")
        parser.feed(html)
        if not parser.rows:
            return []
        header = parser.rows[0]
        value_row = next((row for row in parser.rows[1:] if row and row[0] == trading_day.isoformat()), None)
        if value_row is None:
            return []
        fetched_at = _timestamp()
        result: list[dict[str, str]] = []
        for index, name in enumerate(header[1:], start=1):
            meta = SAFE_PAIR_MAP.get(name)
            if meta is None or meta.pair != DISPLAY_PAIR or index >= len(value_row):
                continue
            raw_rate = value_row[index]
            normalized = _normalize_safe_rate(raw_rate, meta.divisor)
            if normalized == "":
                continue
            result.append(
                {
                    "trade_date": trading_day.isoformat(),
                    "source": SAFE_SOURCE,
                    "pair": meta.pair,
                    "display_name": meta.display_name,
                    "quote_time": "CENTRAL",
                    "rate": normalized,
                    "raw_rate": raw_rate,
                    "quote_basis": meta.quote_basis,
                    "raw_basis": meta.raw_basis,
                    "fetched_at": fetched_at,
                    "derived_from": "",
                }
            )
        return result

    def fetch_cfets_records(self, trading_day: date) -> list[dict[str, str]]:
        date_text = trading_day.strftime("%d %b %Y")
        query = urlencode(
            {
                "lang": "cn",
                "startDateTool": date_text,
                "endDateTool": date_text,
                "currencyCode": "ALL",
            }
        )
        payload = json.loads(
            self._fetch_bytes(
                f"{CFETS_REF_RATE_URL}?{query}",
                data=b"",
                headers=HTTP_HEADERS,
            ).decode("utf-8")
        )
        records = payload.get("records") or []
        fetched_at = _timestamp()
        result: list[dict[str, str]] = []
        for record in records:
            if str(record.get("dealDate") or "") != trading_day.isoformat():
                continue
            pair = str(record.get("ccyPair") or "")
            if pair not in CFETS_SETTLEMENT_PAIRS:
                continue
            hourly_rows: list[tuple[str, str]] = []
            for field, value in record.items():
                matched = re.fullmatch(r"rateOf(\d{2})hour", field)
                if not matched:
                    continue
                hour = int(matched.group(1))
                if hour < DISPLAY_START_HOUR or hour > DISPLAY_END_HOUR:
                    continue
                rate = _normalize_rate(str(value or ""))
                if rate == "":
                    continue
                quote_time = f"{hour:02d}:00"
                hourly_rows.append((quote_time, rate))
                result.append(
                    {
                        "trade_date": trading_day.isoformat(),
                        "source": CFETS_SOURCE,
                        "pair": pair,
                        "display_name": pair,
                        "quote_time": quote_time,
                        "rate": rate,
                        "raw_rate": rate,
                        "quote_basis": "pair_standard",
                        "raw_basis": "pair_standard",
                        "fetched_at": fetched_at,
                        "derived_from": "",
                    }
                )
            if hourly_rows:
                close_time, close_rate = hourly_rows[-1]
                result.append(
                    {
                        "trade_date": trading_day.isoformat(),
                        "source": CFETS_SOURCE,
                        "pair": pair,
                        "display_name": pair,
                        "quote_time": "CLOSE",
                        "rate": close_rate,
                        "raw_rate": close_rate,
                        "quote_basis": "pair_standard",
                        "raw_basis": "pair_standard",
                        "fetched_at": fetched_at,
                        "derived_from": close_time,
                    }
                )
        return result

    def _upsert_rows(self, trading_day: date, sources: set[str], new_rows: list[dict[str, str]]) -> None:
        all_rows: list[dict[str, str]] = []
        if self.csv_path.exists():
            with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                all_rows = [dict(row) for row in reader]
        trade_date_text = trading_day.isoformat()
        filtered = [
            row
            for row in all_rows
            if not (str(row.get("trade_date") or "") == trade_date_text and str(row.get("source") or "") in sources)
        ]
        filtered.extend(new_rows)
        filtered.sort(key=lambda row: (row["trade_date"], row["source"], pair_sort_key(row["pair"]), row["quote_time"]))
        self.csv_path.parent.mkdir(parents=True, exist_ok=True)
        with self.csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(CSV_FIELDS))
            writer.writeheader()
            writer.writerows(filtered)

    @staticmethod
    def _http_request(url: str, data: bytes | None = None, headers: dict[str, str] | None = None) -> bytes:
        request = Request(url, data=data, headers=headers or HTTP_HEADERS)
        with urlopen(request, timeout=20) as response:
            return response.read()


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.replace("\xa0", " ")).strip()


def _timestamp() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _normalize_safe_rate(raw_rate: str, divisor: Decimal) -> str:
    try:
        value = Decimal(raw_rate.replace(",", "").strip())
    except (InvalidOperation, AttributeError):
        return ""
    return _format_decimal(value / divisor)


def _normalize_rate(value: str) -> str:
    if value in {"", "---", "/"}:
        return ""
    try:
        return _format_decimal(Decimal(value))
    except InvalidOperation:
        return ""


def _is_display_hour(value: str) -> bool:
    matched = re.fullmatch(r"(\d{2}):00", value)
    if not matched:
        return False
    hour = int(matched.group(1))
    return DISPLAY_START_HOUR <= hour <= DISPLAY_END_HOUR


def _hour_sort_key(value: str) -> tuple[int, str]:
    matched = re.fullmatch(r"(\d{2}):00", value)
    if matched:
        return int(matched.group(1)), value
    return 99, value


def _format_decimal(value: Decimal) -> str:
    normalized = value.normalize()
    text = format(normalized, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"
