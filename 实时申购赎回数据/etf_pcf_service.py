#!/usr/bin/env python3
"""Daily PCF cache and opportunity classification for the ETF monitor."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import szse_pcf


SHANGHAI = ZoneInfo("Asia/Shanghai")


def _number(value: Any) -> int | float | None:
    text = str(value or "").strip().replace(",", "")
    if not text:
        return None
    try:
        result = Decimal(text)
    except InvalidOperation:
        return None
    if result == result.to_integral_value():
        return int(result)
    return float(result)


def _is_allowed(value: Any) -> bool | None:
    text = str(value or "").strip().upper()
    if not text:
        return None
    if text in {"Y", "YES", "TRUE", "1", "OPEN"}:
        return True
    if text in {"N", "NO", "FALSE", "0", "CLOSED"}:
        return False
    return None


def _iso_day(value: Any, fallback: date) -> str:
    text = str(value or "").strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:]}"
    return fallback.isoformat()


@dataclass
class PcfRecord:
    symbol: str
    requested_day: date
    payload: dict[str, Any]


class PcfService:
    """Synchronous PCF adapter. Network calls are run via ``asyncio.to_thread``."""

    def __init__(
        self,
        cache_dir: Path | str,
        *,
        enabled: bool = True,
        min_request_interval_seconds: int = 8,
        fetch_bytes=None,
    ) -> None:
        self.cache_dir = Path(cache_dir).expanduser().resolve()
        self.enabled = enabled
        self.store = szse_pcf.SzsePcfStore(
            self.cache_dir,
            fetch_bytes=fetch_bytes,
            min_request_interval_seconds=min_request_interval_seconds,
        )
        self.records: dict[str, PcfRecord] = {}

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        code = str(symbol).strip().upper().removesuffix(".SZ")
        if len(code) != 6 or not code.isdigit():
            raise ValueError("PCF 标的必须是 6 位深圳代码")
        return code

    def is_cached(self, symbol: str, trading_day: date) -> bool:
        return self.store.is_fund_detail_cached(
            trading_day, self.normalize_symbol(symbol), szse_pcf.EXCHANGE_SZSE
        )

    def ensure_symbol(
        self,
        symbol: str,
        trading_day: date | None = None,
        *,
        force_refresh: bool = False,
    ) -> dict[str, Any]:
        code = self.normalize_symbol(symbol)
        target_day = trading_day or datetime.now(SHANGHAI).date()
        if not self.enabled:
            payload = self._error_payload(code, target_day, "PCF 自动拉取已关闭")
            self.records[code] = PcfRecord(code, target_day, payload)
            return payload
        try:
            if force_refresh:
                detail = self.store.ensure_fund_detail(
                    target_day,
                    code,
                    force_refresh=True,
                    exchange=szse_pcf.EXCHANGE_SZSE,
                )
            else:
                # The monitor only consumes structured fields/components.  The
                # XML-only path avoids a second optional TXT request.
                self.store.ensure_fund_xml_cached(
                    target_day, code, exchange=szse_pcf.EXCHANGE_SZSE
                )
                detail = self.store.ensure_fund_detail(
                    target_day, code, exchange=szse_pcf.EXCHANGE_SZSE
                )
            payload = self._serialize_detail(code, target_day, detail, status="ready")
        except Exception as exc:
            cached = self._latest_cached_detail(code, target_day)
            if cached is None:
                payload = self._error_payload(code, target_day, str(exc))
            else:
                cached_day, detail = cached
                payload = self._serialize_detail(code, cached_day, detail, status="stale")
                payload["requested_day"] = target_day.isoformat()
                payload["error"] = str(exc)
        self.records[code] = PcfRecord(code, target_day, payload)
        return payload

    def load_cached_symbol(
        self, symbol: str, trading_day: date | None = None
    ) -> dict[str, Any] | None:
        code = self.normalize_symbol(symbol)
        target_day = trading_day or datetime.now(SHANGHAI).date()
        cached = self._latest_cached_detail(code, target_day)
        if cached is None:
            return None
        cached_day, detail = cached
        status = "ready" if cached_day == target_day else "stale"
        payload = self._serialize_detail(code, cached_day, detail, status=status)
        payload["requested_day"] = target_day.isoformat()
        self.records[code] = PcfRecord(code, target_day, payload)
        return payload

    def summary_for(self, symbol: str) -> dict[str, Any]:
        code = self.normalize_symbol(symbol)
        record = self.records.get(code)
        if record is None:
            return {
                "status": "waiting",
                "symbol": code,
                "trading_day": None,
                "creation_redemption_unit": None,
                "creation_allowed": None,
                "redemption_allowed": None,
                "error": None,
            }
        payload = record.payload
        return {
            key: payload.get(key)
            for key in (
                "status",
                "symbol",
                "fund_name",
                "trading_day",
                "requested_day",
                "creation_redemption_unit",
                "creation_allowed",
                "redemption_allowed",
                "creation_limit",
                "redemption_limit",
                "net_creation_limit",
                "net_redemption_limit",
                "component_count",
                "cached_at",
                "error",
            )
        }

    def detail_for(self, symbol: str) -> dict[str, Any] | None:
        record = self.records.get(self.normalize_symbol(symbol))
        return dict(record.payload) if record is not None else None

    def _latest_cached_detail(
        self, code: str, target_day: date
    ) -> tuple[date, Any] | None:
        for offset in range(0, 16):
            candidate = target_day - timedelta(days=offset)
            if not self.store.is_fund_detail_cached(
                candidate, code, szse_pcf.EXCHANGE_SZSE
            ):
                continue
            try:
                detail = self.store.ensure_fund_detail(
                    candidate, code, exchange=szse_pcf.EXCHANGE_SZSE
                )
            except Exception:
                continue
            return candidate, detail
        return None

    @staticmethod
    def _serialize_detail(
        code: str, requested_day: date, detail: Any, *, status: str
    ) -> dict[str, Any]:
        metadata = dict(detail.metadata)
        trading_day = _iso_day(metadata.get("TradingDay"), requested_day)
        ordered_fields = list(szse_pcf.SUMMARY_FIELD_ORDER)
        ordered_fields.extend(key for key in metadata if key not in ordered_fields)
        summary_fields = [
            {
                "field": field,
                "label": szse_pcf.display_summary_label(field),
                "value": metadata.get(field, ""),
            }
            for field in ordered_fields
            if str(metadata.get(field, "")).strip()
        ]
        components = [dict(item) for item in detail.components]
        component_columns = szse_pcf.component_columns(detail.components)
        return {
            "status": status,
            "symbol": code,
            "windcode": f"{code}.SZ",
            "fund_name": detail.fund_name,
            "trading_day": trading_day,
            "requested_day": requested_day.isoformat(),
            "creation_redemption_unit": _number(
                metadata.get("CreationRedemptionUnit")
            ),
            "creation_allowed": _is_allowed(metadata.get("Creation")),
            "redemption_allowed": _is_allowed(metadata.get("Redemption")),
            "creation_limit": _number(metadata.get("CreationLimit")),
            "redemption_limit": _number(metadata.get("RedemptionLimit")),
            "net_creation_limit": _number(metadata.get("NetCreationLimit")),
            "net_redemption_limit": _number(metadata.get("NetRedemptionLimit")),
            "component_count": len(components),
            "cached_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "error": None,
            "summary_fields": summary_fields,
            "component_columns": [
                {
                    "field": field,
                    "label": szse_pcf.display_component_label(field),
                }
                for field in component_columns
            ],
            "components": components,
        }

    @staticmethod
    def _error_payload(code: str, target_day: date, error: str) -> dict[str, Any]:
        return {
            "status": "error",
            "symbol": code,
            "windcode": f"{code}.SZ",
            "fund_name": "",
            "trading_day": None,
            "requested_day": target_day.isoformat(),
            "creation_redemption_unit": None,
            "creation_allowed": None,
            "redemption_allowed": None,
            "creation_limit": None,
            "redemption_limit": None,
            "net_creation_limit": None,
            "net_redemption_limit": None,
            "component_count": 0,
            "cached_at": datetime.now(SHANGHAI).isoformat(timespec="seconds"),
            "error": error,
            "summary_fields": [],
            "component_columns": [],
            "components": [],
        }


def classify_opportunity(
    net_amount: Any,
    pcf: dict[str, Any] | None,
    *,
    reference_day: date | None = None,
) -> dict[str, Any]:
    """Classify direction using share imbalance and PCF basket constraints.

    This is an operational signal, not a profitability calculation.  Counts of
    requests are intentionally ignored because one request may contain multiple
    baskets.
    """

    result: dict[str, Any] = {
        "kind": "waiting",
        "label": "等待数据",
        "actionable": False,
        "net_shares": net_amount if isinstance(net_amount, (int, float)) else None,
        "basket_unit": None,
        "net_baskets": None,
        "full_baskets": 0,
        "pcf_net_limit_shares": None,
        "pcf_net_limit_baskets": None,
        "limit_utilization": None,
        "limit_reached": False,
        "reason": "等待实时份额和当日 PCF",
    }
    if not isinstance(net_amount, (int, float)):
        return result
    if net_amount == 0:
        result.update(kind="flat", label="暂无方向", reason="申购份额与赎回份额相等")
        return result
    if not pcf or pcf.get("status") in {None, "waiting", "error"}:
        result.update(kind="pending", label="待确认", reason="尚无可用 PCF")
        return result
    unit = pcf.get("creation_redemption_unit")
    if not isinstance(unit, (int, float)) or unit <= 0:
        result.update(kind="pending", label="待确认", reason="PCF 缺少最小申赎单位")
        return result
    basket_count = float(net_amount) / float(unit)
    full_baskets = int(abs(basket_count))
    result.update(
        basket_unit=unit,
        net_baskets=basket_count,
        full_baskets=full_baskets,
    )
    target_day = reference_day or datetime.now(SHANGHAI).date()
    if pcf.get("trading_day") != target_day.isoformat() or pcf.get("status") != "ready":
        direction = "申购" if net_amount > 0 else "赎回"
        result.update(
            kind="stale",
            label=f"{direction}倾向（PCF非当日）",
            reason=f"净{direction} {abs(basket_count):.2f} 篮子，但 PCF 日期不是当日",
        )
        return result
    direction = "creation" if net_amount > 0 else "redemption"
    chinese = "申购" if direction == "creation" else "赎回"
    allowed_key = "creation_allowed" if direction == "creation" else "redemption_allowed"
    limit_key = "net_creation_limit" if direction == "creation" else "net_redemption_limit"
    allowed = pcf.get(allowed_key)
    net_limit = pcf.get(limit_key)
    limit_note = ""
    if isinstance(net_limit, (int, float)) and net_limit > 0:
        limit_baskets = float(net_limit) / float(unit)
        utilization = abs(float(net_amount)) / float(net_limit)
        result.update(
            pcf_net_limit_shares=net_limit,
            pcf_net_limit_baskets=limit_baskets,
            limit_utilization=utilization,
            limit_reached=abs(float(net_amount)) >= float(net_limit),
        )
        limit_note = (
            f"；PCF 当日净{chinese}上限 {limit_baskets:.2f} 篮子，"
            f"当前达到 {utilization:.0%}"
        )
    if allowed is False:
        result.update(
            kind="closed",
            label=f"{chinese}关闭",
            reason=f"实时净{chinese} {abs(basket_count):.2f} 篮子，但 PCF 显示{chinese}关闭{limit_note}",
        )
    elif full_baskets < 1:
        result.update(
            kind="partial",
            label=f"{chinese}倾向",
            reason=f"净{chinese}仅 {abs(basket_count):.2f} 篮子，不足一个完整篮子{limit_note}",
        )
    else:
        result.update(
            kind=direction,
            label=f"{chinese}机会",
            actionable=True,
            reason=f"净{chinese} {abs(basket_count):.2f} 篮子（完整 {full_baskets} 篮子）{limit_note}",
        )
    return result


def classify_intraday_opportunity(
    previous: dict[str, Any] | None,
    current: dict[str, Any],
    pcf: dict[str, Any] | None,
    *,
    reference_day: date | None = None,
) -> dict[str, Any]:
    """Classify only capacity released by a *new intraday* reverse flow.

    A static opening imbalance is merely the baseline and is not considered an
    opportunity.  Incremental redemptions may release creation capacity;
    incremental creations may release redemption capacity.
    """

    current_buy = current.get("etfbuyamount")
    current_sell = current.get("etfsellamount")
    current_net = current.get("netamount")
    result: dict[str, Any] = {
        "kind": "baseline",
        "label": "等待盘中变化",
        "actionable": False,
        "net_shares": current_net if isinstance(current_net, (int, float)) else None,
        "basket_unit": None,
        "net_baskets": None,
        "full_baskets": 0,
        "buy_delta": None,
        "sell_delta": None,
        "released_capacity_shares": None,
        "reason": "当前累计申赎仅作为基准，不据此判断机会",
    }
    if previous is None:
        return result
    previous_buy = previous.get("etfbuyamount")
    previous_sell = previous.get("etfsellamount")
    if not all(
        isinstance(value, (int, float))
        for value in (previous_buy, previous_sell, current_buy, current_sell)
    ):
        result.update(kind="waiting", label="等待数据", reason="申购或赎回份额不完整")
        return result
    buy_delta = float(current_buy) - float(previous_buy)
    sell_delta = float(current_sell) - float(previous_sell)
    result.update(buy_delta=buy_delta, sell_delta=sell_delta)
    if buy_delta < 0 or sell_delta < 0:
        result.update(
            kind="baseline",
            label="基准已更新",
            reason="累计份额发生回落，按数据重置或修正处理，不触发机会",
        )
        return result

    # Positive means redemption flow released creation capacity. Negative means
    # creation flow released redemption capacity.
    released = sell_delta - buy_delta
    result["released_capacity_shares"] = released
    if released == 0:
        result.update(
            kind="flat",
            label="盘中变化已抵消",
            reason="申购与赎回份额增量相同，未形成明确的反向容量释放",
        )
        return result

    direction = "creation" if released > 0 else "redemption"
    chinese = "申购" if direction == "creation" else "赎回"
    source = "赎回" if direction == "creation" else "申购"
    signal_shares = abs(released)
    if not pcf or pcf.get("status") in {None, "waiting", "error"}:
        result.update(
            kind="pending",
            label=f"盘中{chinese}信号待确认",
            reason=f"{source}份额盘中净增 {signal_shares:,.0f}，但尚无可用 PCF",
        )
        return result
    unit = pcf.get("creation_redemption_unit")
    if not isinstance(unit, (int, float)) or unit <= 0:
        result.update(
            kind="pending",
            label=f"盘中{chinese}信号待确认",
            reason=f"{source}份额出现反向增量，但 PCF 缺少最小申赎单位",
        )
        return result
    baskets = signal_shares / float(unit)
    full_baskets = int(baskets)
    result.update(
        basket_unit=unit,
        net_baskets=baskets if direction == "creation" else -baskets,
        full_baskets=full_baskets,
    )
    target_day = reference_day or datetime.now(SHANGHAI).date()
    if pcf.get("trading_day") != target_day.isoformat() or pcf.get("status") != "ready":
        result.update(
            kind="stale",
            label=f"盘中{chinese}信号（PCF非当日）",
            reason=f"{source}份额净增 {baskets:.2f} 篮子，可能释放{chinese}额度，但 PCF 非当日",
        )
        return result
    allowed = pcf.get("creation_allowed" if direction == "creation" else "redemption_allowed")
    if allowed is False:
        result.update(
            kind="closed",
            label=f"{chinese}关闭",
            reason=f"{source}份额净增 {baskets:.2f} 篮子，但 PCF 显示{chinese}关闭",
        )
    elif full_baskets < 1:
        result.update(
            kind="partial",
            label=f"盘中{chinese}信号",
            reason=f"{source}份额净增 {baskets:.2f} 篮子，不足一个完整篮子",
        )
    else:
        result.update(
            kind=direction,
            label=f"盘中{chinese}机会",
            actionable=True,
            reason=f"{source}份额盘中净增 {baskets:.2f} 篮子，可能释放 {full_baskets} 个{chinese}篮子额度",
        )
    return result
