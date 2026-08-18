#!/usr/bin/env python3
"""Backfill historical Shanghai ETF PCF files from fund-manager websites.

The Shanghai Stock Exchange download endpoint has no historical-date parameter.
This utility therefore only writes a cache file when the selected fund-manager
source explicitly confirms the requested trading day.  It intentionally permits
partial PCFs: missing fields remain absent and are recorded in the XML metadata.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable


ROOT = Path(__file__).resolve().parent
CALCULATOR_ROOT = ROOT.parent / "赎回收益计算器"
DEFAULT_OUTPUT_ROOT = ROOT / "回补文件"
DEFAULT_RAW_ROOT = ROOT / "原始响应"
DEFAULT_REPORT_ROOT = ROOT / "报告"
DEFAULT_INSTALL_ROOT = CALCULATOR_ROOT / "szse_pcf_cache"

# Source: SSE 2026 annual closing notice.  Keep this explicit rather than
# treating national make-up workdays as exchange trading days.
SSE_CLOSED_DAYS_2026 = frozenset(
    {
        date(2026, 1, 1), date(2026, 1, 2),
        *[date(2026, 2, day) for day in range(16, 24)],
        date(2026, 4, 6),
        *[date(2026, 5, day) for day in range(1, 6)],
        date(2026, 6, 19),
        date(2026, 9, 25),
        *[date(2026, 10, day) for day in range(1, 8)],
    }
)


@dataclass(frozen=True)
class FundSpec:
    code: str
    name: str
    manager: str
    manager_url: str
    provider: str


@dataclass(frozen=True)
class BackfillPayload:
    metadata: dict[str, str]
    components: tuple[dict[str, str], ...]
    source_url: str
    source_kind: str
    raw_files: tuple[tuple[str, str], ...]


# The manager names and official fund URLs are anchored to the Jisilu QDII list.
# Provider "unverified" is deliberately read-only: it creates no XML until the
# manager-specific historical endpoint has been validated.
FUND_SPECS: tuple[FundSpec, ...] = (
    FundSpec("513050", "中概互联网ETF易方达", "易方达", "https://www.efunds.com.cn/fund/513050.shtml", "efunds"),
    FundSpec("513750", "港股通非银ETF广发", "广发", "https://www.gffunds.com.cn/funds/?fundcode=513750", "unverified"),
    FundSpec("513090", "香港证券ETF易方达", "易方达", "https://www.efunds.com.cn/fund/513090.shtml", "efunds"),
    FundSpec("513100", "纳指ETF国泰", "国泰", "https://e.gtfund.com/Etrade/Jijin/view/id/513100", "unverified"),
    FundSpec("513220", "中概互联ETF招商", "招商", "https://www.cmfchina.com/web/fundDetail/513220/index.html", "unverified"),
    FundSpec("513230", "港股通消费ETF华夏", "华夏", "https://www.chinaamc.com/fund/513230/index.shtml", "chinaamc"),
    FundSpec("513520", "日经ETF华夏", "华夏", "https://www.chinaamc.com/fund/513520/index.shtml", "chinaamc"),
    FundSpec("513000", "日经225ETF易方达", "易方达", "https://www.efunds.com.cn/fund/513000.shtml", "efunds"),
    FundSpec("513080", "法国ETF华安", "华安", "https://www.huaan.com.cn/funds/513080/index.shtml", "unverified"),
    FundSpec("513300", "纳斯达克ETF华夏", "华夏", "https://www.chinaamc.com/fund/513300/index.shtml", "chinaamc"),
    FundSpec("513110", "纳指ETF华泰柏瑞", "华泰柏瑞", "https://www.huatai-pb.com/products/zhishu/513110/index.html", "unverified"),
    FundSpec("513880", "日经225ETF华安", "华安", "https://www.huaan.com.cn/funds/513880/index.shtml", "unverified"),
    FundSpec("513850", "美国50ETF易方达", "易方达", "https://www.efunds.com.cn/fund/513850.shtml", "efunds"),
    FundSpec("513030", "德国ETF华安", "华安", "https://www.huaan.com.cn/funds/513030/index.shtml", "unverified"),
    FundSpec("513350", "标普油气ETF富国", "富国", "https://www.fullgoal.com.cn/fundDetail/513350/index.html", "fullgoal_nav"),
    FundSpec("513360", "教育ETF博时", "博时", "https://www.bosera.com/fund/513360.html", "unverified"),
    FundSpec("513650", "标普500ETF南方", "南方", "https://www.nffund.com/new/personal-financing/detail.html?fundCode=513650", "unverified"),
    FundSpec("513400", "道琼斯ETF鹏华", "鹏华", "https://www.phfund.com.cn/fund/fundDetail", "unverified"),
    FundSpec("513870", "纳指ETF富国", "富国", "https://www.fullgoal.com.cn/fundDetail/513870/index.html", "fullgoal_nav"),
    FundSpec("513290", "纳指生物科技ETF汇添富", "汇添富", "https://www.99fund.com/main/products/fund/513290.html", "unverified"),
    FundSpec("513990", "港股通ETF招商", "招商", "https://www.cmfchina.com/web/fundDetail/513990/index.html", "unverified"),
    FundSpec("513500", "标普500ETF博时", "博时", "https://www.bosera.com/fund/513500.html", "unverified"),
    FundSpec("513390", "纳指100ETF博时", "博时", "https://www.bosera.com/fund/513390.html", "unverified"),
)
SPEC_BY_CODE = {spec.code: spec for spec in FUND_SPECS}

MAPPED_FIELDS = (
    "PreTradingDay",
    "CreationRedemptionUnit",
    "PreCashComponent",
    "EstimatedCashComponent",
    "NAVperCU",
    "NAV",
    "MaxCashRatio",
    "Creation",
    "Redemption",
)


class BackfillError(RuntimeError):
    pass


class HistoricalPcfUnavailable(BackfillError):
    pass


class HttpClient:
    def __init__(self, interval_seconds: float) -> None:
        self.interval_seconds = max(0.0, interval_seconds)
        self._last_by_host: dict[str, float] = {}

    def request(self, url: str, *, form: dict[str, str] | None = None) -> str:
        host = urllib.parse.urlparse(url).netloc
        elapsed = time.monotonic() - self._last_by_host.get(host, 0.0)
        if elapsed < self.interval_seconds:
            time.sleep(self.interval_seconds - elapsed)
        if form is None:
            request = urllib.request.Request(url, headers=self._headers())
        else:
            payload = urllib.parse.urlencode(form).encode("utf-8")
            request = urllib.request.Request(
                url,
                data=payload,
                headers={**self._headers(), "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"},
            )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                body = response.read().decode("utf-8-sig", errors="replace")
        except Exception as exc:  # The caller reports one source/date at a time.
            raise BackfillError(f"请求失败 {url}: {exc}") from exc
        self._last_by_host[host] = time.monotonic()
        return body

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/126 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
        }


def compact_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if text in {"", "--", "—", "无", "None", "null"}:
        return ""
    return text.replace(",", "").replace("￥", "").replace("$", "")


def normalized_day(value: Any) -> str:
    text = compact_value(value)
    if re.fullmatch(r"\d+(?:\.\d+)?(?:[Ee][+-]?\d+)?", text):
        try:
            numeric = int(float(text))
        except ValueError:
            numeric = 0
        if 20_000_000 <= numeric <= 21_000_000:
            text = str(numeric)
    digits = re.sub(r"\D", "", text)
    return digits[:8] if len(digits) >= 8 else ""


def text_from_label(rows: Any, *labels: str) -> str:
    if not isinstance(rows, list):
        return ""
    wanted = set(labels)
    for row in rows:
        if isinstance(row, dict) and compact_value(row.get("label")) in wanted:
            return compact_value(row.get("value"))
    return ""


def collect_nonempty(values: dict[str, Any]) -> dict[str, str]:
    return {key: compact_value(value) for key, value in values.items() if compact_value(value)}


def efunds_payload(spec: FundSpec, trading_day: date, http: HttpClient) -> BackfillPayload:
    query = urllib.parse.urlencode({"fundCode": spec.code, "tDate": trading_day.isoformat(), "listType": "1"})
    base_url = f"https://api.efunds.com.cn/xcowch/front/etffund/baseinfo?{query}"
    base_text = http.request(base_url)
    try:
        response = json.loads(base_text)
    except json.JSONDecodeError as exc:
        raise BackfillError(f"易方达返回非 JSON: {exc}") from exc
    data = response.get("data") if isinstance(response, dict) else None
    if not isinstance(data, dict) or not data.get("isMapShow"):
        raise HistoricalPcfUnavailable("易方达该日期没有 PCF")
    actual_day = normalized_day(data.get("tDate") or (data.get("map") or {}).get("TRADINGDAY"))
    if actual_day != trading_day.strftime("%Y%m%d"):
        raise HistoricalPcfUnavailable(f"易方达返回交易日 {actual_day or '空'}，不写入")
    pcf_map = data.get("map") or {}
    info = data.get("etfInfo") or {}
    stock_url = f"https://api.efunds.com.cn/xcowch/front/etffund/stocklist?{query}"
    stock_text = http.request(stock_url)
    try:
        stock_response = json.loads(stock_text)
    except json.JSONDecodeError:
        stock_response = {}
    stock_rows = (stock_response.get("data") or []) if isinstance(stock_response, dict) else []
    components = tuple(
        collect_nonempty(
            {
                "SecurityID": row.get("C_COMPONENTID") or row.get("COMPONENTID") or row.get("C_SECUCODE") or row.get("C_STOCKCODE"),
                "SecurityName": row.get("C_COMPONENTNAME") or row.get("COMPONENTNAME") or row.get("C_SECURITIESNAME") or row.get("C_STOCKSHORT"),
                "ComponentVolume": row.get("N_COMPONENTSHARES") or row.get("COMPONENTSHARES") or row.get("F_SHARES") or row.get("L_NUMBER"),
                "CashSubstitutionMark": row.get("C_CASH_SUBSTITUTION_MARK") or row.get("C_CASHFLAG") or row.get("C_TDBZName"),
                "PremiumRatio": row.get("F_PREMIUM_RATE") or row.get("F_XJTD"),
                "CashSubstitutionAmount": row.get("F_AMOUNT") or row.get("F_TDJE"),
                "Market": row.get("C_EXCHANGEName") or row.get("C_EXCHANGE"),
            }
        )
        for row in stock_rows
        if isinstance(row, dict)
    )
    metadata = collect_nonempty(
        {
            "FundInstrumentID": pcf_map.get("FUNDINSTRUMENTID2") or pcf_map.get("FUNDID1") or info.get("C_FUNDID") or spec.code,
            "FundName": pcf_map.get("FUNDNAME") or info.get("C_FULLNAME") or spec.name,
            "FundManagementCompany": pcf_map.get("FUNDCOMPANYNAME") or info.get("C_FUNDMANAGEMENTCOMPANY"),
            "TradingDay": data.get("tDate"),
            "PreTradingDay": pcf_map.get("PRETRADINGDAY"),
            "CreationRedemptionUnit": pcf_map.get("CREATIONREDEMPTIONUNIT"),
            "PreCashComponent": pcf_map.get("CASHCOMPONENT"),
            "EstimatedCashComponent": pcf_map.get("ESTIMATECASHCOMPONENT"),
            "NAVperCU": pcf_map.get("NAVPERCU"),
            "NAV": pcf_map.get("NAV"),
            "MaxCashRatio": pcf_map.get("MAXCASHRATIO"),
            "Creation": pcf_map.get("CREATIONREDEMPTION"),
            "Redemption": pcf_map.get("CREATIONREDEMPTION"),
            "RecordNum": pcf_map.get("RECORDNUM"),
            "Publish": pcf_map.get("PUBLISH"),
            "UnderlyingSecurityID": pcf_map.get("UNDERLYINGINDEX") or info.get("C_UNDERLYINGINDEX"),
        }
    )
    return BackfillPayload(metadata, components, base_url, "基金公司完整 PCF", (("base.json", base_text), ("components.json", stock_text)))


def chinaamc_payload(spec: FundSpec, trading_day: date, http: HttpClient) -> BackfillPayload:
    url = "https://www.chinaamc.com/front/front/out/etf/tradeList"
    raw = http.request(url, form={"fundCode": spec.code, "queryDate": trading_day.isoformat(), "instType": ""})
    try:
        response = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise BackfillError(f"华夏返回非 JSON: {exc}") from exc
    data = response.get("data") if isinstance(response, dict) else None
    if response.get("status") != 1 or not isinstance(data, dict):
        raise HistoricalPcfUnavailable("华夏该日期没有 PCF")
    actual_day = normalized_day(data.get("secondDate") or data.get("year"))
    if actual_day != trading_day.strftime("%Y%m%d"):
        raise HistoricalPcfUnavailable(f"华夏返回交易日 {actual_day or '空'}，不写入")
    components = tuple(
        collect_nonempty(
            {
                "SecurityID": row.get("stockCode"),
                "SecurityName": row.get("stockName"),
                "ComponentVolume": row.get("shareNumber"),
                "CashSubstitutionMark": row.get("cashSubstitutionMark"),
                "PremiumRatio": row.get("premiumPercentage"),
                "DiscountRatio": row.get("discountPercentage"),
                "CashSubstitutionAmount": row.get("replaceAmount"),
                "Market": row.get("listingMarket"),
            }
        )
        for row in data.get("stockResponseList") or []
        if isinstance(row, dict)
    )
    metadata = collect_nonempty(
        {
            "FundInstrumentID": text_from_label(data.get("baseInfoContent"), "基金代码") or spec.code,
            "FundName": text_from_label(data.get("baseInfoContent"), "基金名称") or spec.name,
            "FundManagementCompany": text_from_label(data.get("baseInfoContent"), "基金管理公司名称"),
            "TradingDay": data.get("secondDate"),
            "PreTradingDay": data.get("firstDate"),
            "PreCashComponent": text_from_label(data.get("firstContent"), "现金差额(单位:元)", "现金差额(单位：元)"),
            "NAVperCU": text_from_label(data.get("firstContent"), "最小申购、赎回单位资产净值(单位:元)", "最小申购、赎回单位资产净值(单位：元)"),
            "NAV": text_from_label(data.get("firstContent"), "基金份额净值(单位:元)", "基金份额净值(单位：元)"),
            "EstimatedCashComponent": text_from_label(data.get("secondContent"), "最小申购、赎回单位的预估现金部分(单位:元)", "最小申购、赎回单位的预估现金部分(单位：元)"),
            "MaxCashRatio": text_from_label(data.get("secondContent"), "现金替代比例上限"),
            "CreationRedemptionUnit": text_from_label(data.get("secondContent"), "最小申购、赎回单位(单位:份)", "最小申购、赎回单位(单位：份)"),
            "Creation": text_from_label(data.get("secondContent"), "申购赎回的允许情况"),
            "Redemption": text_from_label(data.get("secondContent"), "申购赎回的允许情况"),
        }
    )
    return BackfillPayload(metadata, components, url, "基金公司完整 PCF", (("trade_list.json", raw),))


FULLGOAL_NAV_HISTORY: dict[str, tuple[str, str, list[dict[str, Any]]]] = {}
PUBLIC_NAV_HISTORY: dict[str, tuple[str, str, list[dict[str, Any]]]] = {}


def fullgoal_nav_payload(spec: FundSpec, trading_day: date, http: HttpClient) -> BackfillPayload:
    cached = FULLGOAL_NAV_HISTORY.get(spec.code)
    if cached is None:
        query = urllib.parse.urlencode({"productCode": spec.code, "pageNum": "1", "pageSize": "200"})
        url = f"https://www.fullgoal.com.cn/ws-business-server/fund/getFundNavPage?{query}"
        raw = http.request(url)
        try:
            response = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise BackfillError(f"富国返回非 JSON: {exc}") from exc
        raw_rows = ((response.get("data") or {}).get("list") or []) if isinstance(response, dict) else []
        rows = [item for item in raw_rows if isinstance(item, dict)]
        FULLGOAL_NAV_HISTORY[spec.code] = (url, raw, rows)
    else:
        url, raw, rows = cached
    requested_day = trading_day.strftime("%Y%m%d")
    eligible_rows = [
        item
        for item in rows
        if isinstance(item, dict) and normalized_day(item.get("navDate")) and normalized_day(item.get("navDate")) <= requested_day
    ]
    row = max(eligible_rows, key=lambda item: normalized_day(item.get("navDate")), default=None)
    if not isinstance(row, dict):
        raise HistoricalPcfUnavailable("富国该日期没有已公布净值")
    nav_source_day = datetime.strptime(normalized_day(row.get("navDate")), "%Y%m%d").date()
    if (trading_day - nav_source_day).days > 5:
        raise HistoricalPcfUnavailable(f"富国净值来源日 {nav_source_day} 过旧，不写入")
    metadata = collect_nonempty(
        {
            "FundInstrumentID": spec.code,
            "FundName": spec.name,
            "FundManagementCompany": "富国基金管理有限公司",
            # The cache directory is the requested PCF day.  NAV itself keeps
            # its actual source day so this synthetic partial PCF is auditable.
            "TradingDay": trading_day.isoformat(),
            "NAVSourceDay": row.get("navDate"),
            "NAV": row.get("relatePrice") or row.get("adjustNav") or row.get("cumulativeNet"),
        }
    )
    return BackfillPayload(metadata, (), url, "基金公司历史净值映射（部分字段）", (("nav_history.json", raw),))


def public_nav_payload(spec: FundSpec, trading_day: date, http: HttpClient) -> BackfillPayload:
    """Use a clearly marked NAV-only fallback for a manager not yet adapted.

    It is opt-in and never emits cash, unit NAV, component, or limit values.
    The original manager URL remains in the catalog for the next adapter pass.
    """
    cached = PUBLIC_NAV_HISTORY.get(spec.code)
    if cached is None:
        url = f"https://fund.eastmoney.com/pingzhongdata/{spec.code}.js?v=20260711"
        raw = http.request(url)
        match = re.search(r"Data_netWorthTrend\s*=\s*(\[.*?\]);", raw, flags=re.DOTALL)
        if not match:
            raise HistoricalPcfUnavailable("公开净值来源没有历史净值序列")
        try:
            raw_rows = json.loads(match.group(1))
        except json.JSONDecodeError as exc:
            raise BackfillError(f"公开净值来源格式异常: {exc}") from exc
        rows = [item for item in raw_rows if isinstance(item, dict) and item.get("x") is not None and item.get("y") is not None]
        PUBLIC_NAV_HISTORY[spec.code] = (url, raw, rows)
    else:
        url, raw, rows = cached
    requested_day = trading_day.strftime("%Y%m%d")
    dated_rows = []
    for item in rows:
        try:
            nav_day = datetime.utcfromtimestamp(int(item["x"]) / 1000).date()
        except (TypeError, ValueError, OSError):
            continue
        if nav_day.strftime("%Y%m%d") <= requested_day:
            dated_rows.append((nav_day, item))
    if not dated_rows:
        raise HistoricalPcfUnavailable("公开净值来源没有不晚于目标日的数据")
    nav_source_day, row = max(dated_rows, key=lambda item: item[0])
    if (trading_day - nav_source_day).days > 5:
        raise HistoricalPcfUnavailable(f"公开净值来源日 {nav_source_day} 过旧，不写入")
    metadata = collect_nonempty(
        {
            "FundInstrumentID": spec.code,
            "FundName": spec.name,
            "FundManagementCompany": spec.manager,
            "TradingDay": trading_day.isoformat(),
            "NAVSourceDay": nav_source_day.isoformat(),
            "NAV": row.get("y"),
            "BackfillSourceManagerURL": spec.manager_url,
        }
    )
    return BackfillPayload(metadata, (), url, "公开历史净值映射（非基金公司 PCF）", (("public_nav_history.js", raw),))


PROVIDERS: dict[str, Callable[[FundSpec, date, HttpClient], BackfillPayload]] = {
    "efunds": efunds_payload,
    "chinaamc": chinaamc_payload,
    "fullgoal_nav": fullgoal_nav_payload,
}


def write_payload_xml(path: Path, spec: FundSpec, trading_day: date, payload: BackfillPayload) -> None:
    actual_day = normalized_day(payload.metadata.get("TradingDay"))
    expected_day = trading_day.strftime("%Y%m%d")
    if actual_day != expected_day:
        raise BackfillError(f"拒绝写入 {spec.code}: 交易日 {actual_day or '空'} 不等于 {expected_day}")
    root = ET.Element("SSEPortfolioCompositionFile")
    metadata = dict(payload.metadata)
    metadata.setdefault("FundInstrumentID", spec.code)
    metadata.setdefault("FundName", spec.name)
    metadata["TradingDay"] = trading_day.strftime("%Y-%m-%d")
    metadata["BackfillManager"] = spec.manager
    metadata["BackfillSourceURL"] = payload.source_url
    metadata["BackfillDataGrade"] = payload.source_kind
    missing = [key for key in MAPPED_FIELDS if not compact_value(metadata.get(key))]
    if missing:
        metadata["BackfillMissingFields"] = ",".join(missing)
    for key, value in metadata.items():
        if not compact_value(value):
            continue
        # This audit list is text rather than a numeric field; retain commas.
        element_value = str(value).strip() if key == "BackfillMissingFields" else compact_value(value)
        ET.SubElement(root, key).text = element_value
    component_list = ET.SubElement(root, "ComponentList")
    for component in payload.components:
        component_element = ET.SubElement(component_list, "Component")
        for key, value in component.items():
            if compact_value(value):
                ET.SubElement(component_element, key).text = compact_value(value)
    ET.indent(root, space="  ")
    path.parent.mkdir(parents=True, exist_ok=True)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def cache_path(root: Path, trading_day: date, code: str) -> Path:
    return root / trading_day.isoformat() / "sse" / "xml" / f"{code}.xml"


def is_valid_cached(path: Path, trading_day: date) -> bool:
    if not path.is_file():
        return False
    try:
        root = ET.fromstring(path.read_text(encoding="utf-8"))
    except (OSError, ET.ParseError):
        return False
    return normalized_day(root.findtext("TradingDay")) == trading_day.strftime("%Y%m%d")


def write_raw(raw_root: Path, trading_day: date, spec: FundSpec, raw_files: tuple[tuple[str, str], ...]) -> None:
    manager_dir = raw_root / trading_day.isoformat() / re.sub(r"[^A-Za-z0-9_-]", "_", spec.manager)
    manager_dir.mkdir(parents=True, exist_ok=True)
    for suffix, content in raw_files:
        (manager_dir / f"{spec.code}-{suffix}").write_text(content, encoding="utf-8")


def iter_sse_trading_days(start_day: date, end_day: date) -> Iterable[date]:
    current = start_day
    while current <= end_day:
        if current.weekday() < 5 and current not in SSE_CLOSED_DAYS_2026:
            yield current
        current += timedelta(days=1)


def subtract_calendar_months(value: date, months: int) -> date:
    month = value.month - months
    year = value.year
    while month <= 0:
        year -= 1
        month += 12
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def select_specs(codes: str, managers: str) -> tuple[FundSpec, ...]:
    requested_codes = {value.strip() for value in codes.split(",") if value.strip()}
    requested_managers = {value.strip() for value in managers.split(",") if value.strip()}
    selected = [
        spec
        for spec in FUND_SPECS
        if (not requested_codes or spec.code in requested_codes)
        and (not requested_managers or spec.manager in requested_managers)
    ]
    unknown = requested_codes - set(SPEC_BY_CODE)
    if unknown:
        raise SystemExit(f"不在上海重点列表中的代码: {', '.join(sorted(unknown))}")
    return tuple(selected)


def run(args: argparse.Namespace) -> int:
    end_day = args.end
    start_day = args.start or subtract_calendar_months(end_day, args.months)
    if start_day > end_day:
        raise SystemExit("--start 不能晚于 --end")
    output_root = args.output_root.resolve()
    raw_root = args.raw_root.resolve()
    report_root = args.report_root.resolve()
    specs = select_specs(args.codes, args.managers)
    http = HttpClient(args.interval_seconds)
    report: dict[str, Any] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "range": [start_day.isoformat(), end_day.isoformat()],
        "output_root": str(output_root),
        "results": [],
        "unverified_managers": sorted({spec.manager for spec in specs if spec.provider == "unverified"}),
    }
    counts = {"written": 0, "cached": 0, "unavailable": 0, "unverified": 0, "public_nav": 0, "failed": 0}
    if args.purge_closed_days:
        removed = 0
        for root in (output_root, args.install_root.resolve()):
            for closed_day in SSE_CLOSED_DAYS_2026:
                folder = root / closed_day.isoformat() / "sse" / "xml"
                if not folder.is_dir():
                    continue
                for candidate in folder.glob("*.xml"):
                    try:
                        xml_root = ET.fromstring(candidate.read_text(encoding="utf-8"))
                    except (OSError, ET.ParseError):
                        continue
                    if xml_root.findtext("BackfillManager"):
                        candidate.unlink()
                        removed += 1
        print(f"已删除 {removed} 份落在上交所休市日的回补 XML。", flush=True)
        return 0
    if args.install_existing:
        installed = 0
        skipped = 0
        for source in output_root.glob("*/sse/xml/*.xml"):
            try:
                trading_day = date.fromisoformat(source.parents[2].name)
            except ValueError:
                skipped += 1
                continue
            if not is_valid_cached(source, trading_day):
                skipped += 1
                continue
            destination = cache_path(args.install_root.resolve(), trading_day, source.stem)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            installed += 1
        print(f"已安装 {installed} 份已验证回补 XML；跳过 {skipped} 份。", flush=True)
        return 0
    for trading_day in iter_sse_trading_days(start_day, end_day):
        for spec in specs:
            destination = cache_path(output_root, trading_day, spec.code)
            if is_valid_cached(destination, trading_day) and not args.force:
                counts["cached"] += 1
                continue
            provider = PROVIDERS.get(spec.provider)
            using_public_nav = False
            if provider is None and args.include_public_nav_fallback:
                provider = public_nav_payload
                using_public_nav = True
            if provider is None:
                counts["unverified"] += 1
                continue
            try:
                payload = provider(spec, trading_day, http)
                if args.dry_run:
                    result = "would_write"
                else:
                    write_payload_xml(destination, spec, trading_day, payload)
                    write_raw(raw_root, trading_day, spec, payload.raw_files)
                    if args.install:
                        installed = cache_path(args.install_root.resolve(), trading_day, spec.code)
                        installed.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(destination, installed)
                    result = "written"
                    counts["written"] += 1
                    if using_public_nav:
                        counts["public_nav"] += 1
                print(f"{trading_day} {spec.code} {spec.manager}: {result}", flush=True)
                report["results"].append({"date": trading_day.isoformat(), "code": spec.code, "result": result, "grade": payload.source_kind})
            except HistoricalPcfUnavailable as exc:
                counts["unavailable"] += 1
                report["results"].append({"date": trading_day.isoformat(), "code": spec.code, "result": "unavailable", "reason": str(exc)})
            except Exception as exc:
                counts["failed"] += 1
                report["results"].append({"date": trading_day.isoformat(), "code": spec.code, "result": "failed", "reason": str(exc)})
                print(f"{trading_day} {spec.code} {spec.manager}: {exc}", flush=True)
    report["counts"] = counts
    report_root.mkdir(parents=True, exist_ok=True)
    report_path = report_root / f"backfill-{start_day.isoformat()}-{end_day.isoformat()}.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"完成: {counts}; 报告: {report_path}", flush=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="按基金公司历史接口回补上海 ETF PCF；仅写入已核验交易日。")
    parser.add_argument("--start", type=date.fromisoformat, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--end", type=date.fromisoformat, default=date.today(), help="结束日期 YYYY-MM-DD")
    parser.add_argument("--months", type=int, default=3, help="未指定 --start 时回补自然月数")
    parser.add_argument("--codes", default="", help="逗号分隔的上海代码；默认全部")
    parser.add_argument("--managers", default="", help="逗号分隔的基金公司；默认全部")
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--raw-root", type=Path, default=DEFAULT_RAW_ROOT)
    parser.add_argument("--report-root", type=Path, default=DEFAULT_REPORT_ROOT)
    parser.add_argument("--install", action="store_true", help="验证成功后复制到赎回收益计算器缓存")
    parser.add_argument("--install-existing", action="store_true", help="仅安装已生成且交易日校验通过的回补文件，不联网")
    parser.add_argument("--purge-closed-days", action="store_true", help="删除本工具生成且落在上交所休市日的 XML")
    parser.add_argument(
        "--include-public-nav-fallback",
        action="store_true",
        help="为尚未验证基金公司接口的标的写入 NAV-only 回退；XML 会明确标注为非基金公司 PCF",
    )
    parser.add_argument("--install-root", type=Path, default=DEFAULT_INSTALL_ROOT)
    parser.add_argument("--interval-seconds", type=float, default=0.4, help="同一基金公司域名请求间隔")
    parser.add_argument("--force", action="store_true", help="重写已有且交易日匹配的文件")
    parser.add_argument("--dry-run", action="store_true", help="访问来源但不写文件")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
