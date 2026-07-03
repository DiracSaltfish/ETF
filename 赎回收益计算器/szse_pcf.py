from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


LIST_PAGE_URL = "https://www.szse.cn/disclosure/fund/currency/index.html"
LIST_API_URL = "https://www.szse.cn/api/report/ShowReport/data"
REPORTDOCS_BASE_URL = "https://reportdocs.static.szse.cn"
DOWNLOAD_PAGE_BASE_URL = "https://www.szse.cn"
LIST_CATALOG_ID = "sgshqd"
SCHEMA_VERSION = 1
REQUEST_CONTROL_SCHEMA_VERSION = 1
TARGET_FUND_CODE = "159518"
MIN_REQUEST_INTERVAL_SECONDS = 8
LIMIT_COOLDOWN_SECONDS = 10 * 60
TRANSIENT_COOLDOWN_SECONDS = 3 * 60
RATE_LIMIT_STATUS_CODES = {403, 429, 503}
NOT_FOUND_STATUS_CODES = {404, 410}
HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
}

SUMMARY_FIELD_ORDER = (
    "SecurityID",
    "Symbol",
    "FundManagementCompany",
    "UnderlyingSecurityID",
    "TradingDay",
    "PreTradingDay",
    "CreationRedemptionUnit",
    "EstimateCashComponent",
    "CashComponent",
    "NAVperCU",
    "NAV",
    "DividendPerCU",
    "Publish",
    "Creation",
    "Redemption",
    "RecordNum",
    "TotalRecordNum",
    "MaxCashRatio",
    "CreationLimit",
    "RedemptionLimit",
    "CreationLimitPerUser",
    "RedemptionLimitPerUser",
    "NetCreationLimit",
    "NetRedemptionLimit",
    "NetCreationLimitPerUser",
    "NetRedemptionLimitPerUser",
)

SUMMARY_LABELS = {
    "SecurityID": "基金代码",
    "Symbol": "基金简称",
    "FundManagementCompany": "基金管理公司",
    "UnderlyingSecurityID": "目标指数代码",
    "TradingDay": "交易日",
    "PreTradingDay": "前交易日",
    "CreationRedemptionUnit": "最小申赎单位",
    "EstimateCashComponent": "预估现金差额",
    "CashComponent": "现金差额",
    "NAVperCU": "最小申赎单位资产净值",
    "NAV": "基金份额净值",
    "DividendPerCU": "最小申赎单位现金红利",
    "Publish": "是否公布 IOPV",
    "Creation": "是否开放申购",
    "Redemption": "是否开放赎回",
    "RecordNum": "本市场组合证券只数",
    "TotalRecordNum": "全部组合证券只数",
    "MaxCashRatio": "可以现金替代比例上限",
    "CreationLimit": "当天累计可申购上限",
    "RedemptionLimit": "当天累计可赎回上限",
    "CreationLimitPerUser": "单账户累计可申购上限",
    "RedemptionLimitPerUser": "单账户累计可赎回上限",
    "NetCreationLimit": "当天净申购上限",
    "NetRedemptionLimit": "当天净赎回上限",
    "NetCreationLimitPerUser": "单账户净申购上限",
    "NetRedemptionLimitPerUser": "单账户净赎回上限",
}

COMPONENT_FIELD_ORDER = (
    "UnderlyingSecurityID",
    "UnderlyingSymbol",
    "ComponentShare",
    "SubstituteFlag",
    "PremiumRatio",
    "CreationCashSubstitute",
    "RedemptionCashSubstitute",
    "UnderlyingSecurityIDSource",
)

COMPONENT_LABELS = {
    "UnderlyingSecurityID": "证券代码",
    "UnderlyingSymbol": "证券简称",
    "ComponentShare": "股份数量",
    "SubstituteFlag": "现金替代标志",
    "PremiumRatio": "申购现金替代保证金率",
    "CreationCashSubstitute": "申购替代金额",
    "RedemptionCashSubstitute": "赎回替代金额",
    "UnderlyingSecurityIDSource": "代码源",
}

BOOL_VALUE_LABELS = {
    "Y": "是",
    "N": "否",
}

OPEN_VALUE_LABELS = {
    "Y": "开放",
    "N": "禁止",
}

SUBSTITUTE_FLAG_LABELS = {
    "1": "允许",
    "2": "必须",
}


class SzsePcfError(RuntimeError):
    pass


class SzsePcfNotFoundError(SzsePcfError):
    pass


@dataclass(frozen=True)
class PcfListItem:
    fund_code: str
    trade_date: date
    title: str
    page_label: str
    opencode_name: str
    opencode_path: str
    opencode_url: str
    download_page_url: str
    xml_candidate_urls: tuple[str, ...]
    cache_xml_path: str
    cache_txt_path: str

    def to_dict(self) -> dict[str, object]:
        return {
            "fund_code": self.fund_code,
            "trade_date": self.trade_date.isoformat(),
            "title": self.title,
            "page_label": self.page_label,
            "opencode_name": self.opencode_name,
            "opencode_path": self.opencode_path,
            "opencode_url": self.opencode_url,
            "download_page_url": self.download_page_url,
            "xml_candidate_urls": list(self.xml_candidate_urls),
            "cache_xml_path": self.cache_xml_path,
            "cache_txt_path": self.cache_txt_path,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PcfListItem":
        return cls(
            fund_code=str(payload.get("fund_code") or ""),
            trade_date=date.fromisoformat(str(payload.get("trade_date"))),
            title=str(payload.get("title") or ""),
            page_label=str(payload.get("page_label") or ""),
            opencode_name=str(payload.get("opencode_name") or ""),
            opencode_path=str(payload.get("opencode_path") or ""),
            opencode_url=str(payload.get("opencode_url") or ""),
            download_page_url=str(payload.get("download_page_url") or ""),
            xml_candidate_urls=tuple(str(item) for item in (payload.get("xml_candidate_urls") or [])),
            cache_xml_path=str(payload.get("cache_xml_path") or f"xml/{payload.get('fund_code')}.xml"),
            cache_txt_path=str(payload.get("cache_txt_path") or f"txt/{payload.get('fund_code')}.txt"),
        )


@dataclass(frozen=True)
class PcfDayIndex:
    trade_date: date
    fetched_at: str
    source_page_url: str
    source_api_url: str
    record_count: int
    page_count: int
    items: tuple[PcfListItem, ...]

    def find(self, fund_code: str) -> PcfListItem | None:
        for item in self.items:
            if item.fund_code == fund_code:
                return item
        return None

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": SCHEMA_VERSION,
            "trade_date": self.trade_date.isoformat(),
            "fetched_at": self.fetched_at,
            "source_page_url": self.source_page_url,
            "source_api_url": self.source_api_url,
            "record_count": self.record_count,
            "page_count": self.page_count,
            "items": [item.to_dict() for item in self.items],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "PcfDayIndex":
        return cls(
            trade_date=date.fromisoformat(str(payload.get("trade_date"))),
            fetched_at=str(payload.get("fetched_at") or ""),
            source_page_url=str(payload.get("source_page_url") or LIST_PAGE_URL),
            source_api_url=str(payload.get("source_api_url") or ""),
            record_count=int(payload.get("record_count") or 0),
            page_count=int(payload.get("page_count") or 0),
            items=tuple(PcfListItem.from_dict(item) for item in (payload.get("items") or [])),
        )


@dataclass(frozen=True)
class PcfDetail:
    item: PcfListItem
    metadata: dict[str, str]
    components: tuple[dict[str, str], ...]
    xml_path: Path | None
    txt_path: Path | None
    raw_text: str

    @property
    def fund_name(self) -> str:
        return self.metadata.get("Symbol") or self.item.page_label

    @property
    def trading_day(self) -> str:
        return self.metadata.get("TradingDay") or self.item.trade_date.strftime("%Y%m%d")


@dataclass
class RequestControlState:
    schema_version: int = REQUEST_CONTROL_SCHEMA_VERSION
    last_request_at: str = ""
    blocked_until: str = ""
    failure_count: int = 0
    last_error: str = ""
    last_url: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "last_request_at": self.last_request_at,
            "blocked_until": self.blocked_until,
            "failure_count": self.failure_count,
            "last_error": self.last_error,
            "last_url": self.last_url,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "RequestControlState":
        return cls(
            schema_version=int(payload.get("schema_version") or REQUEST_CONTROL_SCHEMA_VERSION),
            last_request_at=str(payload.get("last_request_at") or ""),
            blocked_until=str(payload.get("blocked_until") or ""),
            failure_count=int(payload.get("failure_count") or 0),
            last_error=str(payload.get("last_error") or ""),
            last_url=str(payload.get("last_url") or ""),
        )


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, object]] = []
        self._current: dict[str, object] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        self._current = {"attrs": {key: value or "" for key, value in attrs}, "text": ""}
        self.anchors.append(self._current)

    def handle_data(self, data: str) -> None:
        if self._current is None:
            return
        self._current["text"] = f"{self._current['text']}{data}"

    def handle_endtag(self, tag: str) -> None:
        if tag == "a":
            self._current = None


def display_summary_label(field: str) -> str:
    return SUMMARY_LABELS.get(field, field)


def display_component_label(field: str) -> str:
    return COMPONENT_LABELS.get(field, field)


def display_value(field: str, value: str) -> str:
    if field == "SubstituteFlag":
        return SUBSTITUTE_FLAG_LABELS.get(value, value)
    if field in {"Publish"}:
        return BOOL_VALUE_LABELS.get(value, value)
    if field in {"Creation", "Redemption"}:
        return OPEN_VALUE_LABELS.get(value, value)
    if field in {
        "CreationRedemptionUnit",
        "RecordNum",
        "TotalRecordNum",
        "CreationLimit",
        "RedemptionLimit",
        "CreationLimitPerUser",
        "RedemptionLimitPerUser",
        "NetCreationLimit",
        "NetRedemptionLimit",
        "NetCreationLimitPerUser",
        "NetRedemptionLimitPerUser",
    }:
        return _format_number(value, suffix="份")
    if field in {"EstimateCashComponent", "CashComponent", "NAVperCU", "NAV", "DividendPerCU"}:
        return _format_number(value)
    if field == "MaxCashRatio":
        return _format_ratio(value)
    if field == "TradingDay" or field == "PreTradingDay":
        return _format_yyyymmdd(value)
    if field == "ComponentShare":
        return _format_number(value)
    if field == "PremiumRatio":
        return _format_ratio(value)
    if field in {"CreationCashSubstitute", "RedemptionCashSubstitute"}:
        return _format_number(value)
    return value


def component_columns(components: tuple[dict[str, str], ...]) -> list[str]:
    extras: list[str] = []
    seen = set(COMPONENT_FIELD_ORDER)
    for component in components:
        for field in component:
            if field not in seen and field not in extras:
                extras.append(field)
    return [*COMPONENT_FIELD_ORDER, *extras]


class SzsePcfStore:
    def __init__(
        self,
        root_dir: Path | str,
        fetch_bytes=None,
        *,
        min_request_interval_seconds: int = MIN_REQUEST_INTERVAL_SECONDS,
        limit_cooldown_seconds: int = LIMIT_COOLDOWN_SECONDS,
        transient_cooldown_seconds: int = TRANSIENT_COOLDOWN_SECONDS,
        now_fn=None,
        sleep_fn=None,
    ) -> None:
        self.root_dir = Path(root_dir).expanduser().resolve()
        self._fetch_bytes = fetch_bytes or self._http_get
        self._min_request_interval_seconds = max(0, int(min_request_interval_seconds))
        self._limit_cooldown_seconds = max(0, int(limit_cooldown_seconds))
        self._transient_cooldown_seconds = max(0, int(transient_cooldown_seconds))
        self._now_fn = now_fn or datetime.now
        self._sleep_fn = sleep_fn or time.sleep

    def day_dir(self, trading_day: date) -> Path:
        return self.root_dir / trading_day.isoformat()

    def index_path(self, trading_day: date) -> Path:
        return self.day_dir(trading_day) / "index.json"

    def request_control_path(self) -> Path:
        return self.root_dir / "request_control.json"

    def load_day_index(self, trading_day: date) -> PcfDayIndex | None:
        path = self.index_path(trading_day)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PcfDayIndex.from_dict(payload)

    def load_request_state(self) -> RequestControlState:
        path = self.request_control_path()
        if not path.exists():
            return RequestControlState()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return RequestControlState()
        if not isinstance(payload, dict):
            return RequestControlState()
        return RequestControlState.from_dict(payload)

    def save_request_state(self, state: RequestControlState) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.request_control_path().write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def ensure_day_index(self, trading_day: date, force_refresh: bool = False) -> PcfDayIndex:
        if not force_refresh:
            cached = self.load_day_index(trading_day)
            if cached is not None:
                return cached
        fetched = self.fetch_day_index(trading_day)
        self.save_day_index(fetched)
        return fetched

    def ensure_target_day_index(self, trading_day: date) -> PcfDayIndex:
        cached = self.load_day_index(trading_day)
        if cached is not None and len(cached.items) == 1 and cached.items[0].fund_code == TARGET_FUND_CODE:
            return cached
        index = PcfDayIndex(
            trade_date=trading_day,
            fetched_at=self._timestamp(),
            source_page_url=LIST_PAGE_URL,
            source_api_url="",
            record_count=1,
            page_count=1,
            items=(self.build_target_item(trading_day),),
        )
        self.save_day_index(index)
        return index

    def build_target_item(self, trading_day: date) -> PcfListItem:
        ymd = trading_day.strftime("%Y%m%d")
        opencode_name = f"ETF{TARGET_FUND_CODE}{ymd}.txt"
        opencode_path = f"/files/text/etf/{opencode_name}"
        download_page_url = (
            f"{DOWNLOAD_PAGE_BASE_URL}/modules/report/views/eft_download_new.html"
            f"?path=%2Ffiles%2Ftext%2FETFDown%2F"
            f"&filename=pcf_{TARGET_FUND_CODE}_{ymd}%3B{TARGET_FUND_CODE}ETF{ymd}"
            f"&opencode={opencode_name}"
        )
        return PcfListItem(
            fund_code=TARGET_FUND_CODE,
            trade_date=trading_day,
            title=f"ETF{TARGET_FUND_CODE}申购赎回清单({trading_day:%Y-%m-%d})",
            page_label=f"ETF{TARGET_FUND_CODE}申购赎回清单",
            opencode_name=opencode_name,
            opencode_path=opencode_path,
            opencode_url=urljoin(REPORTDOCS_BASE_URL, opencode_path),
            download_page_url=download_page_url,
            xml_candidate_urls=(
                urljoin(REPORTDOCS_BASE_URL, f"/files/text/ETFDown/pcf_{TARGET_FUND_CODE}_{ymd}.xml"),
                urljoin(REPORTDOCS_BASE_URL, f"/files/text/ETFDown/{TARGET_FUND_CODE}ETF{ymd}.xml"),
            ),
            cache_xml_path=f"xml/{TARGET_FUND_CODE}.xml",
            cache_txt_path=f"txt/{TARGET_FUND_CODE}.txt",
        )

    def save_day_index(self, index: PcfDayIndex) -> None:
        day_dir = self.day_dir(index.trade_date)
        day_dir.mkdir(parents=True, exist_ok=True)
        self.index_path(index.trade_date).write_text(
            json.dumps(index.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def fetch_day_index(self, trading_day: date) -> PcfDayIndex:
        base_query = self._list_query(trading_day, page_no=1)
        first_page = self._load_list_payload(base_query)
        if not first_page:
            return PcfDayIndex(
                trade_date=trading_day,
                fetched_at=self._timestamp(),
                source_page_url=LIST_PAGE_URL,
                source_api_url=self._build_list_url(base_query),
                record_count=0,
                page_count=0,
                items=(),
            )
        metadata = first_page[0].get("metadata") or {}
        page_count = int(metadata.get("pagecount") or 1)
        record_count = int(metadata.get("recordcount") or 0)
        items: list[PcfListItem] = []
        for page_no in range(1, page_count + 1):
            payload = first_page if page_no == 1 else self._load_list_payload(self._list_query(trading_day, page_no))
            for page_item in payload:
                for row in page_item.get("data") or []:
                    html = str((row or {}).get("jjdm") or "")
                    if not html:
                        continue
                    items.append(self._parse_list_item(html, trading_day))
        unique_items = sorted({item.fund_code: item for item in items}.values(), key=lambda item: item.fund_code)
        return PcfDayIndex(
            trade_date=trading_day,
            fetched_at=self._timestamp(),
            source_page_url=LIST_PAGE_URL,
            source_api_url=self._build_list_url(base_query),
            record_count=record_count,
            page_count=page_count,
            items=tuple(unique_items),
        )

    def ensure_detail(
        self,
        trading_day: date,
        fund_code: str,
        force_refresh: bool = False,
    ) -> PcfDetail:
        index = self.ensure_day_index(trading_day, force_refresh=False)
        item = index.find(fund_code)
        if item is None:
            raise SzsePcfNotFoundError(f"{trading_day:%Y-%m-%d} 未找到 {fund_code} 的申购赎回清单")

        day_dir = self.day_dir(trading_day)
        xml_path = day_dir / item.cache_xml_path
        txt_path = day_dir / item.cache_txt_path
        if not force_refresh and xml_path.exists() and txt_path.exists():
            return self._load_detail_from_cache(item, xml_path, txt_path)

        xml_text = None
        if not force_refresh and xml_path.exists():
            xml_text = xml_path.read_text(encoding="utf-8")
        else:
            xml_text = self._fetch_xml_text(item)
            if xml_text is not None:
                xml_path.parent.mkdir(parents=True, exist_ok=True)
                xml_path.write_text(xml_text, encoding="utf-8")

        raw_text = ""
        if not force_refresh and txt_path.exists():
            raw_text = txt_path.read_text(encoding="utf-8")
        else:
            raw_text = self._fetch_raw_text(item)
            if raw_text:
                txt_path.parent.mkdir(parents=True, exist_ok=True)
                txt_path.write_text(raw_text, encoding="utf-8")

        if xml_text:
            return self._detail_from_xml(
                item=item,
                xml_text=xml_text,
                xml_path=xml_path if xml_path.exists() else None,
                txt_path=txt_path if txt_path.exists() else None,
                raw_text=raw_text,
            )
        if raw_text:
            metadata = self._fallback_metadata(item)
            return PcfDetail(
                item=item,
                metadata=metadata,
                components=(),
                xml_path=None,
                txt_path=txt_path if txt_path.exists() else None,
                raw_text=raw_text,
            )
        raise SzsePcfError(f"{trading_day:%Y-%m-%d} {fund_code} 清单抓取失败")

    def ensure_target_detail(self, trading_day: date, force_refresh: bool = False) -> PcfDetail:
        index = self.ensure_target_day_index(trading_day)
        item = index.items[0]
        day_dir = self.day_dir(trading_day)
        xml_path = day_dir / item.cache_xml_path
        txt_path = day_dir / item.cache_txt_path

        if not force_refresh:
            if xml_path.exists():
                xml_text = xml_path.read_text(encoding="utf-8")
                raw_text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
                return self._detail_from_xml(
                    item=item,
                    xml_text=xml_text,
                    xml_path=xml_path,
                    txt_path=txt_path if txt_path.exists() else None,
                    raw_text=raw_text,
                )
            if txt_path.exists():
                raw_text = txt_path.read_text(encoding="utf-8")
                return PcfDetail(
                    item=item,
                    metadata=self._fallback_metadata(item),
                    components=(),
                    xml_path=None,
                    txt_path=txt_path,
                    raw_text=raw_text,
                )

        xml_text = self._fetch_xml_text(item)
        if xml_text:
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_text(xml_text, encoding="utf-8")
            raw_text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
            return self._detail_from_xml(
                item=item,
                xml_text=xml_text,
                xml_path=xml_path,
                txt_path=txt_path if txt_path.exists() else None,
                raw_text=raw_text,
            )

        raw_text = self._fetch_raw_text(item)
        if raw_text:
            txt_path.parent.mkdir(parents=True, exist_ok=True)
            txt_path.write_text(raw_text, encoding="utf-8")
            return PcfDetail(
                item=item,
                metadata=self._fallback_metadata(item),
                components=(),
                xml_path=None,
                txt_path=txt_path,
                raw_text=raw_text,
            )
        raise SzsePcfNotFoundError(f"{trading_day:%Y-%m-%d} {TARGET_FUND_CODE} 暂无可用的申购赎回清单")

    def _load_detail_from_cache(self, item: PcfListItem, xml_path: Path, txt_path: Path) -> PcfDetail:
        xml_text = xml_path.read_text(encoding="utf-8")
        raw_text = txt_path.read_text(encoding="utf-8") if txt_path.exists() else ""
        return self._detail_from_xml(item=item, xml_text=xml_text, xml_path=xml_path, txt_path=txt_path, raw_text=raw_text)

    def _detail_from_xml(
        self,
        *,
        item: PcfListItem,
        xml_text: str,
        xml_path: Path | None,
        txt_path: Path | None,
        raw_text: str,
    ) -> PcfDetail:
        root = ET.fromstring(xml_text)
        metadata: dict[str, str] = {}
        components: list[dict[str, str]] = []
        for child in root:
            tag = self._strip_ns(child.tag)
            if tag == "Components":
                for component in child:
                    component_fields: dict[str, str] = {}
                    for field in component:
                        component_fields[self._strip_ns(field.tag)] = (field.text or "").strip()
                    components.append(component_fields)
                continue
            metadata[tag] = (child.text or "").strip()
        fallback = self._fallback_metadata(item)
        for key, value in fallback.items():
            metadata.setdefault(key, value)
        return PcfDetail(
            item=item,
            metadata=metadata,
            components=tuple(components),
            xml_path=xml_path,
            txt_path=txt_path,
            raw_text=raw_text,
        )

    def _fetch_xml_text(self, item: PcfListItem) -> str | None:
        for url in item.xml_candidate_urls:
            payload = self._safe_fetch(url)
            if not payload or self._looks_like_html(payload):
                continue
            try:
                text = payload.decode("utf-8")
            except UnicodeDecodeError:
                continue
            if text.lstrip().startswith("<?xml") or text.lstrip().startswith("<PCFFile"):
                return text
        return None

    def _fetch_raw_text(self, item: PcfListItem) -> str:
        payload = self._safe_fetch(item.opencode_url)
        if not payload or self._looks_like_html(payload):
            return ""
        return payload.decode("gb18030", errors="replace")

    def _safe_fetch(self, url: str) -> bytes | None:
        try:
            return self._rate_limited_fetch(url)
        except SzsePcfNotFoundError:
            return None
        except SzsePcfError:
            raise
        except Exception:
            return None

    def _rate_limited_fetch(self, url: str) -> bytes:
        self._wait_for_request_slot()
        try:
            payload = self._fetch_bytes(url)
        except HTTPError as exc:
            if exc.code in NOT_FOUND_STATUS_CODES:
                self._record_not_found(url, exc)
                raise SzsePcfNotFoundError(f"{url} 返回 {exc.code}")
            raise self._translate_request_error(url, exc)
        except (URLError, TimeoutError, OSError) as exc:
            raise self._translate_request_error(url, exc)
        except Exception as exc:
            raise self._translate_request_error(url, exc)
        self._record_request_success(url)
        return payload

    def _wait_for_request_slot(self) -> None:
        state = self.load_request_state()
        now = self._now_fn()
        blocked_until = self._parse_timestamp(state.blocked_until)
        if blocked_until is not None and blocked_until > now:
            raise SzsePcfError(
                f"深交所 PCF 拉取处于冷却期，最早可在 {blocked_until:%Y-%m-%d %H:%M:%S} 后重试"
            )
        last_request_at = self._parse_timestamp(state.last_request_at)
        if last_request_at is None or self._min_request_interval_seconds <= 0:
            return
        next_allowed = last_request_at + timedelta(seconds=self._min_request_interval_seconds)
        if next_allowed > now:
            self._sleep_fn((next_allowed - now).total_seconds())

    def _record_request_success(self, url: str) -> None:
        state = RequestControlState(
            last_request_at=self._now_fn().isoformat(timespec="seconds"),
            blocked_until="",
            failure_count=0,
            last_error="",
            last_url=url,
        )
        self.save_request_state(state)

    def _record_not_found(self, url: str, exc: HTTPError) -> None:
        state = RequestControlState(
            last_request_at=self._now_fn().isoformat(timespec="seconds"),
            blocked_until="",
            failure_count=0,
            last_error=f"HTTP {exc.code}",
            last_url=url,
        )
        self.save_request_state(state)

    def _translate_request_error(self, url: str, exc: Exception) -> SzsePcfError:
        now = self._now_fn()
        state = self.load_request_state()
        failure_count = state.failure_count + 1
        blocked_until = ""
        if isinstance(exc, HTTPError) and exc.code in RATE_LIMIT_STATUS_CODES:
            blocked_until = (now + timedelta(seconds=self._limit_cooldown_seconds)).isoformat(timespec="seconds")
            message = (
                f"深交所接口返回 {exc.code}，已暂停拉取到 "
                f"{self._parse_timestamp(blocked_until):%Y-%m-%d %H:%M:%S}"
            )
        elif failure_count >= 2 and self._transient_cooldown_seconds > 0:
            blocked_until = (now + timedelta(seconds=self._transient_cooldown_seconds)).isoformat(timespec="seconds")
            message = (
                f"深交所请求连续失败 {failure_count} 次，已暂停到 "
                f"{self._parse_timestamp(blocked_until):%Y-%m-%d %H:%M:%S}"
            )
        else:
            message = f"深交所请求失败：{exc}"
        self.save_request_state(
            RequestControlState(
                last_request_at=now.isoformat(timespec="seconds"),
                blocked_until=blocked_until,
                failure_count=failure_count,
                last_error=f"{type(exc).__name__}: {exc}",
                last_url=url,
            )
        )
        return SzsePcfError(message)

    @staticmethod
    def _parse_timestamp(value: str) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None

    @staticmethod
    def _timestamp() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _strip_ns(tag: str) -> str:
        return tag.rsplit("}", 1)[-1]

    @staticmethod
    def _looks_like_html(payload: bytes) -> bool:
        probe = payload.lstrip().lower()
        return probe.startswith(b"<!doctype html") or probe.startswith(b"<html")

    @staticmethod
    def _http_get(url: str) -> bytes:
        request = Request(url, headers=HTTP_HEADERS)
        with urlopen(request, timeout=20) as response:
            return response.read()

    def _load_list_payload(self, query: dict[str, str]) -> list[dict[str, object]]:
        raw = self._fetch_bytes(self._build_list_url(query))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list):
            raise SzsePcfError("深交所列表接口返回格式异常")
        return payload

    def _build_list_url(self, query: dict[str, str]) -> str:
        parts = [f"{key}={value}" for key, value in query.items()]
        return f"{LIST_API_URL}?{'&'.join(parts)}"

    @staticmethod
    def _list_query(trading_day: date, page_no: int) -> dict[str, str]:
        day_text = trading_day.isoformat()
        return {
            "SHOWTYPE": "JSON",
            "CATALOGID": LIST_CATALOG_ID,
            "TABKEY": "tab1",
            "txtStart": day_text,
            "txtEnd": day_text,
            "PAGENO": str(page_no),
        }

    def _parse_list_item(self, html: str, expected_day: date) -> PcfListItem:
        parser = _AnchorCollector()
        parser.feed(html)
        if not parser.anchors:
            raise SzsePcfError(f"无法解析列表项: {html}")
        first = parser.anchors[0]
        attrs = dict(first.get("attrs") or {})
        title = str(first.get("text") or "").strip()
        page_label = re.sub(r"\(\d{4}-\d{2}-\d{2}\)$", "", title).strip()
        opencode_path = str(attrs.get("encode-open") or "").strip()
        opencode_name = Path(opencode_path).name
        match = re.search(r"ETF(?P<code>\d{6})(?P<ymd>\d{8})\.txt$", opencode_name, re.IGNORECASE)
        if not match:
            raise SzsePcfError(f"无法从列表项中提取基金代码: {html}")
        fund_code = match.group("code")
        trade_date = datetime.strptime(match.group("ymd"), "%Y%m%d").date()
        if trade_date != expected_day:
            raise SzsePcfError(f"列表项日期异常: {trade_date} != {expected_day}")

        download_page_url = ""
        xml_candidate_urls: tuple[str, ...] = ()
        if len(parser.anchors) > 1:
            download_attrs = dict(parser.anchors[1].get("attrs") or {})
            href = str(download_attrs.get("href") or "").strip()
            if href:
                download_page_url = urljoin(DOWNLOAD_PAGE_BASE_URL, href)
                xml_candidate_urls = tuple(self._build_xml_candidates(download_page_url))

        return PcfListItem(
            fund_code=fund_code,
            trade_date=trade_date,
            title=title,
            page_label=page_label,
            opencode_name=opencode_name,
            opencode_path=opencode_path,
            opencode_url=urljoin(REPORTDOCS_BASE_URL, opencode_path),
            download_page_url=download_page_url,
            xml_candidate_urls=xml_candidate_urls,
            cache_xml_path=f"xml/{fund_code}.xml",
            cache_txt_path=f"txt/{fund_code}.txt",
        )

    @staticmethod
    def _build_xml_candidates(download_page_url: str) -> list[str]:
        parsed = urlparse(download_page_url)
        query = parse_qs(parsed.query)
        base_path = str((query.get("path") or [""])[0] or "")
        if base_path and not base_path.endswith("/"):
            base_path = f"{base_path}/"
        filenames = str((query.get("filename") or [""])[0] or "")
        names = [item for item in filenames.split(";") if item]
        urls: list[str] = []
        for name in names:
            urls.append(urljoin(REPORTDOCS_BASE_URL, f"{base_path}{name}.xml"))
        return urls

    @staticmethod
    def _fallback_metadata(item: PcfListItem) -> dict[str, str]:
        return {
            "SecurityID": item.fund_code,
            "Symbol": item.page_label,
            "TradingDay": item.trade_date.strftime("%Y%m%d"),
        }


def _format_number(value: str, suffix: str = "") -> str:
    if value == "":
        return value
    if re.fullmatch(r"-?\d+", value):
        return f"{int(value):,}{suffix}"
    if re.fullmatch(r"-?\d+\.\d+", value):
        number = float(value)
        text = f"{number:,.4f}".rstrip("0").rstrip(".")
        return f"{text}{suffix}"
    return f"{value}{suffix}"


def _format_ratio(value: str) -> str:
    if value == "":
        return value
    try:
        return f"{float(value) * 100:.2f}%"
    except ValueError:
        return value


def _format_yyyymmdd(value: str) -> str:
    if re.fullmatch(r"\d{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    return value
