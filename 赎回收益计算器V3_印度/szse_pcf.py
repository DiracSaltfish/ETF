from __future__ import annotations

import json
import re
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urljoin, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


LIST_PAGE_URL = "https://www.szse.cn/disclosure/fund/currency/index.html"
LIST_API_URL = "https://www.szse.cn/api/report/ShowReport/data"
REPORTDOCS_BASE_URL = "https://reportdocs.static.szse.cn"
DOWNLOAD_PAGE_BASE_URL = "https://www.szse.cn"
LIST_CATALOG_ID = "sgshqd"
SSE_LIST_PAGE_URL = "https://www.sse.com.cn/disclosure/fund/etflist/"
SSE_QUERY_API_URL = "https://query.sse.com.cn/commonQuery.do"
SSE_DOWNLOAD_URL = "https://query.sse.com.cn/etfDownload/downloadETF2Bulletin.do"
EXCHANGE_SZSE = "SZSE"
EXCHANGE_SSE = "SSE"
SHANGHAI = ZoneInfo("Asia/Shanghai")
SSE_ETF_CLASS_CODES = ("01", "03", "08", "09", "31", "02", "37", "32", "33", "06")
SCHEMA_VERSION = 1
REQUEST_CONTROL_SCHEMA_VERSION = 1
TARGET_FUND_CODE = "159518"
FOCUS_FUND_DISPLAY_NAMES = {
    "159518": "标普油气ETF嘉实",
    "159501": "纳指ETF嘉实",
    "159502": "标普生物科技ETF嘉实",
    "159509": "纳指科技ETF景顺",
    "159513": "纳斯达克100ETF大成",
    "159529": "标普消费ETF景顺",
    "159561": "德国ETF嘉实",
    "159567": "港股创新药ETF银华",
    "159570": "港股通创新药ETF汇添富",
    "159577": "美国50ETF汇添富",
    "159605": "中概互联ETF广发",
    "159607": "中概互联网ETF嘉实",
    "159612": "标普500ETF国泰",
    "159615": "恒生生物科技ETF南方",
    "159632": "纳斯达克ETF华安",
    "159655": "标普500ETF华夏",
    "159659": "纳斯达克100ETF招商",
    "159660": "纳指ETF汇添富",
    "159696": "纳指ETF易方达",
    "159751": "港股通科技ETF鹏华",
    "159792": "港股通互联网ETF富国",
    "159866": "日经ETF工银",
    "159941": "纳指ETF广发",
}
FOCUS_FUND_CODES = tuple(FOCUS_FUND_DISPLAY_NAMES)
SSE_FUND_DISPLAY_NAMES = {
    "513050": "中概互联",
    "513750": "港股非银",
    "513090": "香港证券",
    "513100": "纳指ETF",
    "513220": "互联网30",
    "513230": "H股消费",
    "513520": "日经ETF",
    "513000": "225ETF",
    "513080": "法国ETF",
    "513300": "纳斯达克",
    "513110": "纳指100",
    "513880": "日经225",
    "513850": "美国50",
    "513030": "德国ETF",
    "513350": "油气ETF",
    "513360": "教育ETF",
    "513650": "标普ETF",
    "513400": "道琼斯",
    "513870": "纳指指数",
    "513290": "纳指生物",
    "513990": "港股通综",
    "513500": "标普500",
    "513390": "纳指基金",
}
SSE_FUND_CODES = tuple(SSE_FUND_DISPLAY_NAMES)
FOCUS_FUND_KEYS = tuple(f"SZ{code}" for code in FOCUS_FUND_CODES) + tuple(f"SH{code}" for code in SSE_FUND_CODES)
MIN_REQUEST_INTERVAL_SECONDS = 8
LIMIT_COOLDOWN_SECONDS = 10 * 60
TRANSIENT_COOLDOWN_SECONDS = 3 * 60
RATE_LIMIT_STATUS_CODES = {403, 429, 503}
NOT_FOUND_STATUS_CODES = {404, 410}

# Foreground and startup-prefetch workers share the same cache. Requests to a
# single exchange remain serialized, while SZSE and SSE keep separate slots.
_REQUEST_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_REQUEST_LOCKS_GUARD = threading.Lock()

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

SSE_SUMMARY_FIELD_ORDER = (
    "FundInstrumentID",
    "FundName",
    "FundManagementCompany",
    "TradingDay",
    "PreTradingDay",
    "CreationRedemptionUnit",
    "EstimatedCashComponent",
    "PreCashComponent",
    "NAVperCU",
    "NAV",
    "MaxCashRatio",
    "CreationLimit",
    "RedemptionLimit",
    "NetCreationLimit",
    "NetRedemptionLimit",
    "NetCreationLimitPerAcct",
    "NetRedemptionLimitPerAcct",
    "CreationLimitPerAcct",
    "RedemptionLimitPerAcct",
    "PublishIOPVFlag",
    "CreationRedemptionSwitch",
    "CreationRedemptionMechanism",
    "RecordNumber",
)

SSE_SUMMARY_LABELS = {
    "FundInstrumentID": "基金代码",
    "FundName": "基金名称",
    "FundManagementCompany": "基金管理公司",
    "TradingDay": "交易日",
    "PreTradingDay": "前交易日",
    "CreationRedemptionUnit": "最小申赎单位",
    "EstimatedCashComponent": "预估现金部分",
    "PreCashComponent": "现金差额",
    "NAVperCU": "最小申赎单位净值",
    "NAV": "基金份额净值",
    "MaxCashRatio": "现金替代比例上限",
    "CreationLimit": "累计可申购上限",
    "RedemptionLimit": "累计可赎回上限",
    "NetCreationLimit": "净申购上限",
    "NetRedemptionLimit": "净赎回上限",
    "NetCreationLimitPerAcct": "单账户净申购上限",
    "NetRedemptionLimitPerAcct": "单账户净赎回上限",
    "CreationLimitPerAcct": "单账户累计申购上限",
    "RedemptionLimitPerAcct": "单账户累计赎回上限",
    "PublishIOPVFlag": "是否公布 IOPV",
    "CreationRedemptionSwitch": "申购赎回允许情况",
    "CreationRedemptionMechanism": "申购赎回模式",
    "RecordNumber": "成分证券数量",
}

SSE_COMPONENT_FIELD_ORDER = (
    "InstrumentID",
    "InstrumentName",
    "Quantity",
    "SubstitutionFlag",
    "CreationPremiumRate",
    "RedemptionDiscountRate",
    "SubstitutionCashAmount",
    "Market",
)

SSE_COMPONENT_LABELS = {
    "InstrumentID": "证券代码",
    "InstrumentName": "证券简称",
    "Quantity": "数量",
    "SubstitutionFlag": "现金替代标志",
    "CreationPremiumRate": "申购现金替代溢价比例",
    "RedemptionDiscountRate": "赎回现金替代折价比例",
    "SubstitutionCashAmount": "替代金额",
    "Market": "挂牌市场",
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

SSE_SUBSTITUTION_FLAG_LABELS = {
    "0": "禁止",
    "1": "允许",
    "2": "必须",
    "3": "深市或京市退补",
    "4": "深市或京市必须",
    "5": "退补",
    "6": "必须",
    "7": "港市退补",
    "8": "港市必须",
}

SSE_PUBLISH_IOPV_LABELS = {"1": "是", "0": "否"}
SSE_CREATION_REDEMPTION_SWITCH_LABELS = {
    "0": "禁止申购赎回",
    "1": "允许申购和赎回",
    "2": "仅允许申购",
    "3": "仅允许赎回",
}
SSE_CREATION_REDEMPTION_MECHANISM_LABELS = {
    "0": "现金申赎",
    "1": "沪市成分证券实物对价",
    "2": "沪市、深市成分证券实物对价",
    "3": "银行间市场债券实物对价",
}
SSE_MARKET_LABELS = {
    "101": "上海证券交易所",
    "102": "深圳证券交易所",
    "103": "香港联合交易所",
    "105": "外汇交易中心",
    "106": "北京证券交易所",
    "9999": "其他",
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
    exchange: str = EXCHANGE_SZSE

    def to_dict(self) -> dict[str, object]:
        return {
            "fund_code": self.fund_code,
            "exchange": self.exchange,
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
            exchange=normalize_exchange(str(payload.get("exchange") or EXCHANGE_SZSE)),
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

    def find(self, fund_code: str, exchange: str | None = None) -> PcfListItem | None:
        target = normalize_fund_code(fund_code)
        target_exchange = normalize_exchange(exchange) if exchange else ""
        for item in self.items:
            if target_exchange and normalize_exchange(item.exchange) != target_exchange:
                continue
            if normalize_fund_code(item.fund_code) == target:
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
        return self.metadata.get("Symbol") or self.metadata.get("FundName") or self.item.page_label

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


def display_value(field: str, value: str, exchange: str = EXCHANGE_SZSE) -> str:
    if normalize_exchange(exchange) == EXCHANGE_SSE:
        if field == "SubstitutionFlag":
            return SSE_SUBSTITUTION_FLAG_LABELS.get(value, value)
        if field == "PublishIOPVFlag":
            return SSE_PUBLISH_IOPV_LABELS.get(value, value)
        if field == "CreationRedemptionSwitch":
            return SSE_CREATION_REDEMPTION_SWITCH_LABELS.get(value, value)
        if field == "CreationRedemptionMechanism":
            return SSE_CREATION_REDEMPTION_MECHANISM_LABELS.get(value, value)
        if field == "Market":
            return SSE_MARKET_LABELS.get(value, value)
        if field in {"TradingDay", "PreTradingDay"}:
            return _format_yyyymmdd(value)
        if field in {
            "CreationRedemptionUnit",
            "CreationLimit",
            "RedemptionLimit",
            "NetCreationLimit",
            "NetRedemptionLimit",
            "NetCreationLimitPerAcct",
            "NetRedemptionLimitPerAcct",
            "CreationLimitPerAcct",
            "RedemptionLimitPerAcct",
            "RecordNumber",
            "Quantity",
        }:
            return _format_number(value, suffix="份" if field != "Quantity" else "")
        if field in {
            "EstimatedCashComponent",
            "PreCashComponent",
            "NAVperCU",
            "NAV",
            "SubstitutionCashAmount",
        }:
            return _format_number(value)
        if field in {"MaxCashRatio", "CreationPremiumRate", "RedemptionDiscountRate"}:
            return _format_ratio(value)
        return value

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


def normalize_fund_code(value: str) -> str:
    text = str(value or "").strip().upper()
    if text.startswith("SSE"):
        text = text[3:]
    if text.startswith("SZ") or text.startswith("SH"):
        text = text[2:]
    return re.sub(r"\D", "", text)


def normalize_exchange(value: str | None, default: str = EXCHANGE_SZSE) -> str:
    text = str(value or "").strip().upper()
    if text in {"SH", "SSE", EXCHANGE_SSE}:
        return EXCHANGE_SSE
    if text in {"SZ", "SZSE", EXCHANGE_SZSE}:
        return EXCHANGE_SZSE
    return default


def exchange_prefix(exchange: str) -> str:
    return "SH" if normalize_exchange(exchange) == EXCHANGE_SSE else "SZ"


def fund_key(exchange: str, fund_code: str) -> str:
    return f"{exchange_prefix(exchange)}{normalize_fund_code(fund_code)}"


def parse_fund_key(value: str, default_exchange: str | None = None) -> tuple[str, str]:
    text = str(value or "").strip().upper()
    if text.startswith("SH") or text.startswith("SSE"):
        return EXCHANGE_SSE, normalize_fund_code(text)
    if text.startswith("SZ") or text.startswith("SZSE"):
        return EXCHANGE_SZSE, normalize_fund_code(text)
    code = normalize_fund_code(text)
    if code in SSE_FUND_DISPLAY_NAMES and code not in FOCUS_FUND_DISPLAY_NAMES:
        return EXCHANGE_SSE, code
    if code in FOCUS_FUND_DISPLAY_NAMES:
        return EXCHANGE_SZSE, code
    return normalize_exchange(default_exchange or EXCHANGE_SZSE), code


def is_focus_fund(exchange: str, fund_code: str) -> bool:
    code = normalize_fund_code(fund_code)
    normalized_exchange = normalize_exchange(exchange)
    if normalized_exchange == EXCHANGE_SSE:
        return code in SSE_FUND_DISPLAY_NAMES
    return code in FOCUS_FUND_DISPLAY_NAMES


def display_fund_name(fund_code: str, fallback: str = "", exchange: str | None = None) -> str:
    code = normalize_fund_code(fund_code)
    if normalize_exchange(exchange or "") == EXCHANGE_SSE or code in SSE_FUND_DISPLAY_NAMES:
        return SSE_FUND_DISPLAY_NAMES.get(code) or fallback or code
    return FOCUS_FUND_DISPLAY_NAMES.get(code) or fallback or code


def focus_fund_items(items: tuple[PcfListItem, ...]) -> tuple[PcfListItem, ...]:
    by_key = {fund_key(item.exchange, item.fund_code): item for item in items}
    return tuple(by_key[key] for key in FOCUS_FUND_KEYS if key in by_key)


def interleave_pcf_items(items: tuple[PcfListItem, ...] | list[PcfListItem]) -> tuple[PcfListItem, ...]:
    """Return SSE/SZSE PCFs in alternating order, beginning with SSE."""
    queues = {
        EXCHANGE_SSE: [item for item in items if normalize_exchange(item.exchange) == EXCHANGE_SSE],
        EXCHANGE_SZSE: [item for item in items if normalize_exchange(item.exchange) == EXCHANGE_SZSE],
    }
    ordered: list[PcfListItem] = []
    while queues[EXCHANGE_SSE] or queues[EXCHANGE_SZSE]:
        for exchange in (EXCHANGE_SSE, EXCHANGE_SZSE):
            if queues[exchange]:
                ordered.append(queues[exchange].pop(0))
    return tuple(ordered)


def display_summary_label(field: str, exchange: str = EXCHANGE_SZSE) -> str:
    if normalize_exchange(exchange) == EXCHANGE_SSE:
        return SSE_SUMMARY_LABELS.get(field, field)
    return SUMMARY_LABELS.get(field, field)


def display_component_label(field: str, exchange: str = EXCHANGE_SZSE) -> str:
    if normalize_exchange(exchange) == EXCHANGE_SSE:
        return SSE_COMPONENT_LABELS.get(field, field)
    return COMPONENT_LABELS.get(field, field)


def component_columns(components: tuple[dict[str, str], ...], exchange: str = EXCHANGE_SZSE) -> list[str]:
    if normalize_exchange(exchange) == EXCHANGE_SSE:
        base_order = SSE_COMPONENT_FIELD_ORDER
    else:
        base_order = COMPONENT_FIELD_ORDER
    extras: list[str] = []
    seen = set(base_order)
    for component in components:
        for field in component:
            if field not in seen and field not in extras:
                extras.append(field)
    return [*base_order, *extras]


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

    def request_control_path(self, exchange: str = EXCHANGE_SZSE) -> Path:
        if normalize_exchange(exchange) == EXCHANGE_SSE:
            return self.root_dir / "request_control_sse.json"
        # Keep the original SZSE path so existing caches retain their history.
        return self.root_dir / "request_control.json"

    def load_day_index(self, trading_day: date) -> PcfDayIndex | None:
        path = self.index_path(trading_day)
        if not path.exists():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        return PcfDayIndex.from_dict(payload)

    def load_request_state(self, exchange: str = EXCHANGE_SZSE) -> RequestControlState:
        path = self.request_control_path(exchange)
        if not path.exists():
            return RequestControlState()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return RequestControlState()
        if not isinstance(payload, dict):
            return RequestControlState()
        return RequestControlState.from_dict(payload)

    def save_request_state(self, state: RequestControlState, exchange: str = EXCHANGE_SZSE) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.request_control_path(exchange).write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def is_fund_detail_cached(
        self,
        trading_day: date,
        fund_code: str,
        exchange: str = EXCHANGE_SZSE,
    ) -> bool:
        normalized_exchange, code = parse_fund_key(fund_code, default_exchange=exchange)
        if not code:
            return False
        item = self.build_fund_item(trading_day, code, exchange=normalized_exchange)
        xml_path = self.day_dir(trading_day) / item.cache_xml_path
        if not xml_path.is_file():
            return False
        if normalized_exchange != EXCHANGE_SSE:
            return True
        try:
            detail = self._detail_from_sse_xml(
                item=item,
                xml_text=xml_path.read_text(encoding="utf-8"),
                xml_path=xml_path,
            )
        except (OSError, ET.ParseError):
            return False
        return self._sse_detail_matches_trading_day(detail, trading_day)

    def ensure_fund_xml_cached(
        self,
        trading_day: date,
        fund_code: str,
        exchange: str = EXCHANGE_SZSE,
    ) -> Path:
        """Cache the XML PCF only, without downloading the optional raw TXT.

        Historical bulk backfills need the structured PCF data, while fetching
        raw TXT as well doubles the request count for SZSE.  The UI and all
        calculation paths treat the XML file as the cache-complete artifact.
        """
        normalized_exchange, code = parse_fund_key(fund_code, default_exchange=exchange)
        if not code:
            raise SzsePcfError("基金代码不能为空")
        item = self.build_fund_item(trading_day, code, exchange=normalized_exchange)
        xml_path = self.day_dir(trading_day) / item.cache_xml_path
        if self.is_fund_detail_cached(trading_day, code, normalized_exchange):
            return xml_path
        if normalized_exchange == EXCHANGE_SSE:
            detail = self.ensure_sse_fund_detail(trading_day, code)
            if detail.xml_path is None:
                raise SzsePcfNotFoundError(f"{trading_day:%Y-%m-%d} 上交所 {code} 暂无可用 XML PCF")
            return detail.xml_path
        xml_text = self._fetch_xml_text(item)
        if not xml_text:
            exchange_label = "上交所" if normalized_exchange == EXCHANGE_SSE else "深交所"
            raise SzsePcfNotFoundError(
                f"{trading_day:%Y-%m-%d} {exchange_label} {code} 暂无可用 XML PCF"
            )
        xml_path.parent.mkdir(parents=True, exist_ok=True)
        xml_path.write_text(xml_text, encoding="utf-8")
        return xml_path

    def missing_focus_items(self, trading_day: date) -> tuple[PcfListItem, ...]:
        index = self.build_focus_day_index(trading_day)
        return tuple(
            item
            for item in index.items
            if not self.is_fund_detail_cached(trading_day, item.fund_code, item.exchange)
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
        if cached is not None:
            target_item = cached.find(TARGET_FUND_CODE)
            if target_item is not None:
                return PcfDayIndex(
                    trade_date=trading_day,
                    fetched_at=cached.fetched_at,
                    source_page_url=cached.source_page_url,
                    source_api_url=cached.source_api_url,
                    record_count=1,
                    page_count=1,
                    items=(target_item,),
                )
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

    def build_fund_item(
        self,
        trading_day: date,
        fund_code: str,
        page_label: str = "",
        exchange: str = EXCHANGE_SZSE,
    ) -> PcfListItem:
        if normalize_exchange(exchange) == EXCHANGE_SSE:
            return self.build_sse_fund_item(trading_day, fund_code, page_label)
        code = normalize_fund_code(fund_code)
        if not code:
            raise SzsePcfError("基金代码不能为空")
        ymd = trading_day.strftime("%Y%m%d")
        display_name = display_fund_name(code, page_label, EXCHANGE_SZSE)
        opencode_name = f"ETF{code}{ymd}.txt"
        opencode_path = f"/files/text/etf/{opencode_name}"
        download_page_url = (
            f"{DOWNLOAD_PAGE_BASE_URL}/modules/report/views/eft_download_new.html"
            f"?path=%2Ffiles%2Ftext%2FETFDown%2F"
            f"&filename=pcf_{code}_{ymd}%3B{code}ETF{ymd}"
            f"&opencode={opencode_name}"
        )
        return PcfListItem(
            fund_code=code,
            trade_date=trading_day,
            title=f"{display_name}申购赎回清单({trading_day:%Y-%m-%d})",
            page_label=display_name,
            opencode_name=opencode_name,
            opencode_path=opencode_path,
            opencode_url=urljoin(REPORTDOCS_BASE_URL, opencode_path),
            download_page_url=download_page_url,
            xml_candidate_urls=(
                urljoin(REPORTDOCS_BASE_URL, f"/files/text/ETFDown/pcf_{code}_{ymd}.xml"),
                urljoin(REPORTDOCS_BASE_URL, f"/files/text/ETFDown/{code}ETF{ymd}.xml"),
            ),
            cache_xml_path=f"xml/{code}.xml",
            cache_txt_path=f"txt/{code}.txt",
            exchange=EXCHANGE_SZSE,
        )

    def build_sse_fund_item(self, trading_day: date, fund_code: str, page_label: str = "") -> PcfListItem:
        code = normalize_fund_code(fund_code)
        if not code:
            raise SzsePcfError("基金代码不能为空")
        display_name = display_fund_name(code, page_label, EXCHANGE_SSE)
        download_url = f"{SSE_DOWNLOAD_URL}?{urlencode({'fundCode': code})}"
        return PcfListItem(
            fund_code=code,
            trade_date=trading_day,
            title=f"{display_name}申购赎回清单({trading_day:%Y-%m-%d})",
            page_label=display_name,
            opencode_name=f"ssepcf_{code}.xml",
            opencode_path="",
            opencode_url=download_url,
            download_page_url=download_url,
            xml_candidate_urls=(download_url,),
            cache_xml_path=f"sse/xml/{code}.xml",
            cache_txt_path="",
            exchange=EXCHANGE_SSE,
        )

    def build_target_item(self, trading_day: date) -> PcfListItem:
        return self.build_fund_item(trading_day, TARGET_FUND_CODE)

    def build_focus_day_index(self, trading_day: date) -> PcfDayIndex:
        items = tuple(self.build_fund_item(trading_day, code, exchange=EXCHANGE_SZSE) for code in FOCUS_FUND_CODES)
        items = items + tuple(self.build_sse_fund_item(trading_day, code) for code in SSE_FUND_CODES)
        return PcfDayIndex(
            trade_date=trading_day,
            fetched_at=self._timestamp(),
            source_page_url=f"{LIST_PAGE_URL} | {SSE_LIST_PAGE_URL}",
            source_api_url="",
            record_count=len(items),
            page_count=1,
            items=items,
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

    def fetch_sse_day_index(self, trading_day: date) -> PcfDayIndex:
        items: dict[str, PcfListItem] = {}
        page_count_total = 0
        for etf_class in SSE_ETF_CLASS_CODES:
            page_no = 1
            while True:
                payload = self._load_sse_query_payload(self._sse_list_query(etf_class, page_no))
                page_help = payload.get("pageHelp") if isinstance(payload.get("pageHelp"), dict) else {}
                page_count = int(page_help.get("pageCount") or 1)
                page_count_total += 1
                rows = payload.get("result") or page_help.get("data") or []
                for row in rows:
                    if not isinstance(row, dict):
                        continue
                    code = normalize_fund_code(str(row.get("FUNDID2") or ""))
                    if not code:
                        continue
                    name = str(row.get("ETF_FULLNAME") or row.get("FUND_NAME") or "")
                    items[code] = self.build_sse_fund_item(trading_day, code, name)
                if page_no >= page_count:
                    break
                page_no += 1
        return PcfDayIndex(
            trade_date=trading_day,
            fetched_at=self._timestamp(),
            source_page_url=SSE_LIST_PAGE_URL,
            source_api_url=SSE_QUERY_API_URL,
            record_count=len(items),
            page_count=page_count_total,
            items=tuple(sorted(items.values(), key=lambda item: item.fund_code)),
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

    def ensure_fund_detail(
        self,
        trading_day: date,
        fund_code: str,
        force_refresh: bool = False,
        exchange: str = EXCHANGE_SZSE,
    ) -> PcfDetail:
        if normalize_exchange(exchange) == EXCHANGE_SSE:
            return self.ensure_sse_fund_detail(trading_day, fund_code, force_refresh=force_refresh)
        code = normalize_fund_code(fund_code)
        item = self.build_fund_item(trading_day, code, exchange=EXCHANGE_SZSE)
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
        raise SzsePcfNotFoundError(f"{trading_day:%Y-%m-%d} {code} 暂无可用的申购赎回清单")

    def ensure_sse_fund_detail(
        self,
        trading_day: date,
        fund_code: str,
        force_refresh: bool = False,
    ) -> PcfDetail:
        code = normalize_fund_code(fund_code)
        item = self.build_sse_fund_item(trading_day, code)
        day_dir = self.day_dir(trading_day)
        xml_path = day_dir / item.cache_xml_path
        latest_download_day = self._latest_sse_download_day()
        if xml_path.exists():
            try:
                cached_detail = self._detail_from_sse_xml(
                    item=item,
                    xml_text=xml_path.read_text(encoding="utf-8"),
                    xml_path=xml_path,
                )
            except (OSError, ET.ParseError):
                cached_detail = None
            if (
                cached_detail is not None
                and self._sse_detail_matches_trading_day(cached_detail, trading_day)
                and (not force_refresh or trading_day != latest_download_day)
            ):
                return cached_detail

        if trading_day != latest_download_day:
            raise SzsePcfNotFoundError(
                f"{trading_day:%Y-%m-%d} {code} 未找到已验证的上交所历史 PCF；"
                "上交所下载接口不接受交易日期，程序不会用最新清单回填历史日期"
            )

        xml_text = self._fetch_xml_text(item)
        if xml_text:
            detail = self._detail_from_sse_xml(item=item, xml_text=xml_text, xml_path=None)
            if not self._sse_detail_matches_trading_day(detail, trading_day):
                actual_day = detail.metadata.get("TradingDay") or "未知日期"
                raise SzsePcfNotFoundError(
                    f"上交所返回 {code} 的 PCF 交易日为 {actual_day}，与请求日期 "
                    f"{trading_day:%Y-%m-%d} 不一致，未写入缓存"
                )
            xml_path.parent.mkdir(parents=True, exist_ok=True)
            xml_path.write_text(xml_text, encoding="utf-8")
            return self._detail_from_sse_xml(item=item, xml_text=xml_text, xml_path=xml_path)
        raise SzsePcfNotFoundError(f"{trading_day:%Y-%m-%d} {code} 暂无可用的上交所申购赎回清单")

    @staticmethod
    def _sse_detail_matches_trading_day(detail: PcfDetail, trading_day: date) -> bool:
        actual = re.sub(r"\D", "", str(detail.metadata.get("TradingDay") or ""))
        return actual == trading_day.strftime("%Y%m%d")

    @staticmethod
    def _latest_sse_download_day() -> date:
        current = datetime.now(SHANGHAI).date()
        while current.weekday() >= 5:
            current -= timedelta(days=1)
        return current

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

    def _detail_from_sse_xml(self, *, item: PcfListItem, xml_text: str, xml_path: Path | None) -> PcfDetail:
        root = ET.fromstring(xml_text)
        metadata: dict[str, str] = {}
        components: list[dict[str, str]] = []
        for child in root:
            tag = self._strip_ns(child.tag)
            if tag == "ComponentList":
                for component in child:
                    component_fields: dict[str, str] = {}
                    for field in component:
                        field_name = self._strip_ns(field.tag)
                        if field_name == "UnderlyingSecurityID":
                            component_fields["Market"] = (field.text or "").strip()
                        else:
                            component_fields[field_name] = (field.text or "").strip()
                    components.append(component_fields)
                continue
            metadata[tag] = (child.text or "").strip()

        metadata.setdefault("FundInstrumentID", item.fund_code)
        metadata.setdefault("FundName", item.page_label)
        metadata.setdefault("SecurityID", metadata.get("FundInstrumentID", item.fund_code))
        metadata.setdefault("Symbol", metadata.get("FundName", item.page_label))
        if "PreCashComponent" in metadata:
            metadata.setdefault("CashComponent", metadata["PreCashComponent"])
        if "EstimatedCashComponent" in metadata:
            metadata.setdefault("EstimateCashComponent", metadata["EstimatedCashComponent"])
        return PcfDetail(
            item=item,
            metadata=metadata,
            components=tuple(components),
            xml_path=xml_path,
            txt_path=None,
            raw_text=xml_text,
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
            if (
                text.lstrip().startswith("<?xml")
                or text.lstrip().startswith("<PCFFile")
                or text.lstrip().startswith("<SSEPortfolioCompositionFile")
            ):
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
        exchange = self._exchange_for_url(url)
        with self._request_lock(exchange):
            self._wait_for_request_slot(exchange)
            try:
                payload = self._fetch_bytes(url)
            except HTTPError as exc:
                if exc.code in NOT_FOUND_STATUS_CODES:
                    self._record_not_found(url, exc, exchange)
                    raise SzsePcfNotFoundError(f"{url} 返回 {exc.code}")
                raise self._translate_request_error(url, exc, exchange)
            except (URLError, TimeoutError, OSError) as exc:
                raise self._translate_request_error(url, exc, exchange)
            except Exception as exc:
                raise self._translate_request_error(url, exc, exchange)
            self._record_request_success(url, exchange)
            return payload

    def _wait_for_request_slot(self, exchange: str) -> None:
        state = self.load_request_state(exchange)
        now = self._now_fn()
        blocked_until = self._parse_timestamp(state.blocked_until)
        if blocked_until is not None and blocked_until > now:
            raise SzsePcfError(
                f"交易所 PCF 拉取处于冷却期，最早可在 {blocked_until:%Y-%m-%d %H:%M:%S} 后重试"
            )
        last_request_at = self._parse_timestamp(state.last_request_at)
        if last_request_at is None or self._min_request_interval_seconds <= 0:
            return
        next_allowed = last_request_at + timedelta(seconds=self._min_request_interval_seconds)
        if next_allowed > now:
            self._sleep_fn((next_allowed - now).total_seconds())

    def _record_request_success(self, url: str, exchange: str) -> None:
        state = RequestControlState(
            last_request_at=self._now_fn().isoformat(timespec="seconds"),
            blocked_until="",
            failure_count=0,
            last_error="",
            last_url=url,
        )
        self.save_request_state(state, exchange)

    def _record_not_found(self, url: str, exc: HTTPError, exchange: str) -> None:
        state = RequestControlState(
            last_request_at=self._now_fn().isoformat(timespec="seconds"),
            blocked_until="",
            failure_count=0,
            last_error=f"HTTP {exc.code}",
            last_url=url,
        )
        self.save_request_state(state, exchange)

    def _translate_request_error(self, url: str, exc: Exception, exchange: str) -> SzsePcfError:
        now = self._now_fn()
        state = self.load_request_state(exchange)
        failure_count = state.failure_count + 1
        blocked_until = ""
        if isinstance(exc, HTTPError) and exc.code in RATE_LIMIT_STATUS_CODES:
            blocked_until = (now + timedelta(seconds=self._limit_cooldown_seconds)).isoformat(timespec="seconds")
            message = (
                f"交易所接口返回 {exc.code}，已暂停拉取到 "
                f"{self._parse_timestamp(blocked_until):%Y-%m-%d %H:%M:%S}"
            )
        elif failure_count >= 2 and self._transient_cooldown_seconds > 0:
            blocked_until = (now + timedelta(seconds=self._transient_cooldown_seconds)).isoformat(timespec="seconds")
            message = (
                f"交易所请求连续失败 {failure_count} 次，已暂停到 "
                f"{self._parse_timestamp(blocked_until):%Y-%m-%d %H:%M:%S}"
            )
        else:
            message = f"交易所请求失败：{exc}"
        self.save_request_state(
            RequestControlState(
                last_request_at=now.isoformat(timespec="seconds"),
                blocked_until=blocked_until,
                failure_count=failure_count,
                last_error=f"{type(exc).__name__}: {exc}",
                last_url=url,
            ),
            exchange,
        )
        return SzsePcfError(message)

    def _request_lock(self, exchange: str) -> threading.Lock:
        key = (str(self.root_dir), normalize_exchange(exchange))
        with _REQUEST_LOCKS_GUARD:
            lock = _REQUEST_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _REQUEST_LOCKS[key] = lock
            return lock

    @staticmethod
    def _exchange_for_url(url: str) -> str:
        host = urlparse(url).netloc.lower()
        if host.endswith("sse.com.cn"):
            return EXCHANGE_SSE
        return EXCHANGE_SZSE

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
        headers = dict(HTTP_HEADERS)
        if "query.sse.com.cn" in url:
            headers["Referer"] = SSE_LIST_PAGE_URL
        request = Request(url, headers=headers)
        with urlopen(request, timeout=20) as response:
            return response.read()

    def _load_list_payload(self, query: dict[str, str]) -> list[dict[str, object]]:
        raw = self._rate_limited_fetch(self._build_list_url(query))
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, list):
            raise SzsePcfError("深交所列表接口返回格式异常")
        return payload

    def _load_sse_query_payload(self, query: dict[str, str]) -> dict[str, object]:
        raw = self._rate_limited_fetch(f"{SSE_QUERY_API_URL}?{urlencode(query)}")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise SzsePcfError("上交所查询接口返回格式异常")
        return payload

    def _build_list_url(self, query: dict[str, str]) -> str:
        parts = [f"{key}={value}" for key, value in query.items()]
        return f"{LIST_API_URL}?{'&'.join(parts)}"

    @staticmethod
    def _sse_list_query(etf_class: str, page_no: int) -> dict[str, str]:
        page_text = str(page_no)
        return {
            "isPagination": "true",
            "pageHelp.pageSize": "100",
            "pageHelp.pageNo": page_text,
            "pageHelp.beginPage": page_text,
            "pageHelp.cacheSize": "1",
            "pageHelp.endPage": page_text,
            "sqlId": "COMMON_SSE_PL_ETFGGSGSHQD_L",
            "ETF_CLASS": etf_class,
            "type": "inParams",
            "FUND_CODE": "",
            "KEY_WORDS": "",
        }

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
            exchange=EXCHANGE_SZSE,
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
        metadata = {
            "SecurityID": item.fund_code,
            "Symbol": item.page_label,
            "TradingDay": item.trade_date.strftime("%Y%m%d"),
        }
        if normalize_exchange(item.exchange) == EXCHANGE_SSE:
            metadata.update(
                {
                    "FundInstrumentID": item.fund_code,
                    "FundName": item.page_label,
                }
            )
        return metadata


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
