from __future__ import annotations

import csv
import getpass
import json
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import ib_flex


NEW_YORK = ZoneInfo("America/New_York")
AUTO_CSV_NAME = "ib_activity_auto.csv"
METADATA_NAME = "ib_activity_auto.metadata.json"
TRADE_HEADER = (
    "交易",
    "Header",
    "DataDiscriminator",
    "资产分类",
    "货币",
    "代码",
    "日期/时间",
    "数量",
    "交易价格",
    "收盘价格",
    "收益",
    "佣金/税",
    "基础",
    "已实现的损益",
    "按市值计算的损益",
    "代码",
)
ASSET_CATEGORY_NAMES = {
    "STK": "股票",
    "OPT": "期权",
    "FOP": "期权",
    "FUT": "期货",
    "CASH": "外汇",
    "BOND": "债券",
    "CFD": "差价合约",
    "FUND": "基金",
    "WAR": "权证",
}


@dataclass(frozen=True)
class ActivityRecord:
    row: tuple[str, ...]
    trade_datetime: datetime
    unique_key: tuple[str, ...]


@dataclass(frozen=True)
class AutoRefreshResult:
    csv_path: Path
    requested_start: date
    requested_end: date
    actual_end: date | None
    row_count: int
    refreshed: bool
    warning: str = ""

    def status_text(self) -> str:
        latest = self.actual_end.isoformat() if self.actual_end else "无成交"
        prefix = "Flex 已更新" if self.refreshed else "Flex 更新失败，正在使用缓存"
        return f"{prefix}：{self.row_count} 条成交，最新成交日 {latest}"


def cached_csv_path(cache_dir: Path | str) -> Path:
    return Path(cache_dir).expanduser() / AUTO_CSV_NAME


def metadata_path(cache_dir: Path | str) -> Path:
    return Path(cache_dir).expanduser() / METADATA_NAME


def latest_available_date(now: datetime | None = None) -> date:
    """Return the most recent completed US business day.

    Flex activity statements are end-of-day reports. Requesting the previous
    weekday avoids treating the still-open/current US trading day as complete.
    Exchange holidays are harmless: Flex simply returns no records for them,
    and the actual latest record date is recorded separately.
    """

    current = now or datetime.now(NEW_YORK)
    if current.tzinfo is None:
        current = current.replace(tzinfo=NEW_YORK)
    else:
        current = current.astimezone(NEW_YORK)
    candidate = current.date() - timedelta(days=1)
    while candidate.weekday() >= 5:
        candidate -= timedelta(days=1)
    return candidate


def iter_date_chunks(
    start_date: date,
    end_date: date,
    maximum_days: int = 365,
) -> Iterable[tuple[date, date]]:
    if maximum_days < 1:
        raise ValueError("maximum_days 必须至少为 1")
    if end_date < start_date:
        raise ValueError("Flex 起始日期不能晚于最近可拉取日")
    cursor = start_date
    while cursor <= end_date:
        chunk_end = min(end_date, cursor + timedelta(days=maximum_days - 1))
        yield cursor, chunk_end
        cursor = chunk_end + timedelta(days=1)


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _decimal_text(value: str, fallback: str = "0") -> str:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return fallback
    try:
        return format(Decimal(text), "f")
    except InvalidOperation:
        return fallback


def _parse_flex_datetime(value: str, trade_date: str = "") -> datetime:
    text = str(value or "").strip()
    candidates = (
        (text, "%Y%m%d;%H%M%S"),
        (text, "%Y%m%d;%H%M%S%f"),
        (text, "%Y-%m-%d, %H:%M:%S"),
        (text, "%Y-%m-%d;%H%M%S"),
        (text, "%Y%m%d"),
        (str(trade_date or "").strip(), "%Y%m%d"),
        (str(trade_date or "").strip(), "%Y-%m-%d"),
    )
    for candidate, pattern in candidates:
        if not candidate:
            continue
        try:
            return datetime.strptime(candidate, pattern)
        except ValueError:
            continue
    raise ValueError(f"无法识别 Flex 成交时间：{text or trade_date}")


def _record_key(tag_name: str, attributes: dict[str, str]) -> tuple[str, ...]:
    symbol = str(attributes.get("symbol") or "").strip().upper()
    moment = str(attributes.get("dateTime") or attributes.get("orderTime") or "").strip()
    account = str(attributes.get("accountId") or "").strip()
    for field in ("ibOrderID", "tradeID", "transactionID", "ibExecID"):
        identifier = str(attributes.get(field) or "").strip()
        if identifier:
            return (
                tag_name,
                account,
                field,
                identifier,
                symbol,
                moment,
                str(attributes.get("quantity") or "").strip(),
                str(attributes.get("tradePrice") or "").strip(),
            )
    return (
        tag_name,
        account,
        symbol,
        moment,
        str(attributes.get("quantity") or "").strip(),
        str(attributes.get("tradePrice") or "").strip(),
        str(attributes.get("proceeds") or attributes.get("tradeMoney") or "").strip(),
        str(attributes.get("ibCommission") or "").strip(),
    )


def _activity_record(element: ET.Element) -> ActivityRecord | None:
    attributes = {str(key): str(value) for key, value in element.attrib.items()}
    symbol = str(attributes.get("symbol") or "").strip().upper()
    quantity = _decimal_text(str(attributes.get("quantity") or ""), "")
    price = _decimal_text(str(attributes.get("tradePrice") or ""), "")
    if not symbol or not quantity or not price:
        return None
    try:
        if Decimal(quantity) == 0:
            return None
    except InvalidOperation:
        return None
    try:
        trade_datetime = _parse_flex_datetime(
            str(attributes.get("dateTime") or attributes.get("orderTime") or ""),
            str(attributes.get("tradeDate") or ""),
        )
    except ValueError:
        return None

    proceeds = str(attributes.get("proceeds") or attributes.get("tradeMoney") or "").strip()
    if not proceeds:
        proceeds = format(Decimal(quantity) * Decimal(price), "f")
    asset_category = str(attributes.get("assetCategory") or "").strip().upper()
    tag_name = _local_name(element.tag)
    row = (
        "交易",
        "Data",
        tag_name,
        ASSET_CATEGORY_NAMES.get(asset_category, asset_category or "其他"),
        str(attributes.get("currency") or "").strip().upper(),
        symbol,
        trade_datetime.strftime("%Y-%m-%d, %H:%M:%S"),
        quantity,
        price,
        _decimal_text(str(attributes.get("closePrice") or "")),
        _decimal_text(proceeds),
        _decimal_text(str(attributes.get("ibCommission") or "")),
        _decimal_text(str(attributes.get("cost") or "")),
        _decimal_text(str(attributes.get("fifoPnlRealized") or "")),
        _decimal_text(str(attributes.get("mtmPnl") or "")),
        str(attributes.get("notes") or "").strip(),
    )
    return ActivityRecord(
        row=row,
        trade_datetime=trade_datetime,
        unique_key=_record_key(tag_name, attributes),
    )


def extract_activity_records(payload: bytes) -> list[ActivityRecord]:
    if not payload.lstrip().startswith(b"<"):
        raise ib_flex.FlexError("自动数据源要求 Flex Query 输出 XML")
    try:
        root = ET.fromstring(payload)
    except ET.ParseError as exc:
        raise ib_flex.FlexError(f"Flex 下载结果不是有效 XML：{exc}") from None

    service_response = ib_flex.parse_service_response(payload)
    if service_response is not None:
        if service_response.status.lower() != "success":
            code = service_response.error_code or "unknown"
            message = service_response.error_message or "未知错误"
            raise ib_flex.FlexError(f"IBKR Flex 返回错误 {code}：{message}")
        raise ib_flex.FlexError("IBKR Flex 尚未返回完整活动报表")

    orders = [element for element in root.iter() if _local_name(element.tag) == "Order"]
    elements = orders or [element for element in root.iter() if _local_name(element.tag) == "Trade"]
    records: list[ActivityRecord] = []
    for element in elements:
        record = _activity_record(element)
        if record is not None:
            records.append(record)
    return records


def merge_activity_records(records: Iterable[ActivityRecord]) -> list[ActivityRecord]:
    unique: dict[tuple[str, ...], ActivityRecord] = {}
    for record in records:
        unique.setdefault(record.unique_key, record)
    return sorted(
        unique.values(),
        key=lambda item: (item.trade_datetime, item.row[5], item.unique_key),
    )


def write_activity_csv(records: Iterable[ActivityRecord], destination: Path | str) -> Path:
    output_path = Path(destination).expanduser()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_suffix(output_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(TRADE_HEADER)
        for record in records:
            writer.writerow(record.row)
    temporary.replace(output_path)
    return output_path


def _write_metadata(cache_dir: Path | str, payload: dict[str, object]) -> None:
    path = metadata_path(cache_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def load_metadata(cache_dir: Path | str) -> dict[str, object]:
    path = metadata_path(cache_dir)
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _cached_result(
    cache_dir: Path | str,
    start_date: date,
    end_date: date,
    warning: str,
) -> AutoRefreshResult | None:
    path = cached_csv_path(cache_dir)
    if not path.is_file():
        return None
    metadata = load_metadata(cache_dir)
    actual_end: date | None = None
    try:
        actual_end_text = str(metadata.get("actual_end") or "")
        actual_end = date.fromisoformat(actual_end_text) if actual_end_text else None
    except ValueError:
        actual_end = None
    try:
        row_count = int(metadata.get("row_count") or 0)
    except (TypeError, ValueError):
        row_count = 0
    return AutoRefreshResult(
        csv_path=path,
        requested_start=start_date,
        requested_end=end_date,
        actual_end=actual_end,
        row_count=row_count,
        refreshed=False,
        warning=warning,
    )


def refresh_flex_csv(
    start_date: date,
    query_id: str,
    cache_dir: Path | str,
    *,
    end_date: date | None = None,
    configured_token: str = "",
    token_env: str = "IBKR_FLEX_TOKEN",
    keychain_service: str = ib_flex.DEFAULT_KEYCHAIN_SERVICE,
    keychain_account: str | None = None,
    downloader: Callable[[str, str, date, date], bytes] = ib_flex.download_statement,
) -> AutoRefreshResult:
    requested_end = end_date or latest_available_date()
    output_path = cached_csv_path(cache_dir)
    try:
        chunks = list(iter_date_chunks(start_date, requested_end))
        clean_query_id = str(query_id or "").strip()
        if not clean_query_id:
            raise ValueError("Flex Query ID 未配置")
        token = str(configured_token or "").strip()
        if not token:
            token, _token_source = ib_flex.load_token(
                token_env,
                keychain_service,
                keychain_account or getpass.getuser(),
            )
        if not token:
            raise ValueError(
                "Flex Token 未配置；请在数据源设置中填写，"
                f"或通过环境变量 {token_env}/macOS 钥匙串 {keychain_service} 提供"
            )

        collected: list[ActivityRecord] = []
        for chunk_start, chunk_end in chunks:
            payload = downloader(token, clean_query_id, chunk_start, chunk_end)
            collected.extend(extract_activity_records(payload))
        records = merge_activity_records(collected)
        write_activity_csv(records, output_path)
        actual_end = max((item.trade_datetime.date() for item in records), default=None)
        _write_metadata(
            cache_dir,
            {
                "requested_start": start_date.isoformat(),
                "requested_end": requested_end.isoformat(),
                "actual_end": actual_end.isoformat() if actual_end else "",
                "row_count": len(records),
                "updated_at_utc": datetime.now(timezone.utc).isoformat(),
                "query_id": clean_query_id,
            },
        )
        return AutoRefreshResult(
            csv_path=output_path,
            requested_start=start_date,
            requested_end=requested_end,
            actual_end=actual_end,
            row_count=len(records),
            refreshed=True,
        )
    except Exception as exc:
        warning = f"IB 自动更新失败，已使用最近一次本地缓存：{exc}"
        cached = _cached_result(cache_dir, start_date, requested_end, warning)
        if cached is not None:
            return cached
        raise
