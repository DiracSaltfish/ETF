#!/usr/bin/env python3
"""Reproducible China-internet ETF creation/redemption arbitrage study.

The script deliberately separates two evidence grades:

1. ``private_exact``: SZ159605 dates where 1navs exposes dated PCF component
   Bid/Ask basket values.  The domestic ETF leg is still a public last price,
   so adverse tick and non-basket cost scenarios are applied.
2. ``public_screen``: all four funds' public minute ``estnav``/``pmp`` rows.
   These are candidate screens, not executable arbitrage backtests.

All timestamps are Asia/Shanghai.  Python execution for this project is
expected to use ``conda run -n ag python``.
"""

from __future__ import annotations

import argparse
import json
import math
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import requests


SERVER = "https://1navs.com"
TICK_CNY = 0.001
ENTRY_BUFFER_BPS = 10.0
SCENARIOS = {
    "optimistic": {"adverse_ticks": 0.5, "nonbasket_cost_bps": 15.0},
    "base": {"adverse_ticks": 1.0, "nonbasket_cost_bps": 35.0},
    "stress": {"adverse_ticks": 2.0, "nonbasket_cost_bps": 70.0},
}
KWEB_HOLDING_WEIGHTS_PCT = {
    "700": 10.53, "9988": 8.27, "PDD": 8.22, "3690": 7.22, "9999": 6.76,
    "YMM": 4.05, "9888": 4.04, "9618": 3.88, "1024": 3.81, "2423": 3.81,
    "6618": 3.60, "9961": 3.53, "9626": 3.53, "TME": 3.32, "BZ": 3.28,
    "TAL": 2.96, "VIPS": 2.37, "3888": 2.09, "241": 1.71, "136": 1.36,
    "780": 1.36, "JOYY": 1.29, "1357": 1.01, "2400": 0.98, "1797": 0.97,
    "6060": 0.96, "QFIN": 0.85, "ATHM": 0.84, "772": 0.83, "9899": 0.75,
    "1833": 0.61, "WB": 0.45, "1060": 0.44,
}


@dataclass(frozen=True)
class Fund:
    symbol: str
    code: str
    name: str
    exchange: str
    prospectus: str
    pcf_file: str


FUNDS = (
    Fund(
        "SZ159605",
        "159605",
        "中概互联ETF广发",
        "SZSE",
        "159605_中概互联ETF广发_更新招募说明书_2026-07-06.pdf",
        "159605_中概互联ETF广发_PCF_2026-07-10.xml",
    ),
    Fund(
        "SZ159607",
        "159607",
        "中概互联网ETF嘉实",
        "SZSE",
        "159607_中概互联网ETF嘉实_更新招募说明书_2026-01-23.pdf",
        "159607_中概互联网ETF嘉实_PCF_2026-07-10.xml",
    ),
    Fund(
        "SH513050",
        "513050",
        "中概互联ETF易方达",
        "SSE",
        "513050_中概互联_更新招募说明书_2026-06-01.pdf",
        "513050_中概互联_PCF_2026-07-10.xml",
    ),
    Fund(
        "SH513220",
        "513220",
        "互联网30（招商）",
        "SSE",
        "513220_互联网30_更新招募说明书_2026-07-03.pdf",
        "513220_互联网30_PCF_2026-07-10.xml",
    ),
)
FUND_BY_SYMBOL = {fund.symbol: fund for fund in FUNDS}


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(node: ET.Element, name: str, default: str = "") -> str:
    for child in node:
        if local_name(child.tag) == name:
            return str(child.text or "").strip()
    return default


def finite_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime)):
        return value.isoformat()
    if isinstance(value, np.generic):
        return json_safe(value.item())
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def market_from_source(source: str, identifier: str) -> str:
    source = str(source).strip()
    identifier = str(identifier).strip().upper()
    if source == "9999" or (identifier and not identifier.isdigit()):
        return "US"
    if source == "103":
        return "HK"
    if source in {"101", "102"}:
        return "CN"
    return "OTHER"


def parse_pcf_snapshot(path: Path, fund: Fund) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    root = ET.fromstring(path.read_bytes())
    rows: list[dict[str, Any]] = []
    if fund.exchange == "SZSE":
        unit = float(child_text(root, "CreationRedemptionUnit"))
        nav_per_cu = float(child_text(root, "NAVperCU"))
        estimate_cash = float(child_text(root, "EstimateCashComponent"))
        creation_open = child_text(root, "Creation") == "Y"
        redemption_open = child_text(root, "Redemption") == "Y"
        creation_limit = finite_float(child_text(root, "CreationLimit")) or 0.0
        redemption_limit = finite_float(child_text(root, "RedemptionLimit")) or 0.0
        net_creation_limit = finite_float(child_text(root, "NetCreationLimit")) or 0.0
        net_redemption_limit = finite_float(child_text(root, "NetRedemptionLimit")) or 0.0
        trading_day = child_text(root, "TradingDay")
        upfront_cash = None
        for component in root.iter():
            if local_name(component.tag) != "Component":
                continue
            fields = {local_name(child.tag): str(child.text or "").strip() for child in component}
            identifier = fields.get("UnderlyingSecurityID", "")
            name = fields.get("UnderlyingSymbol", "")
            quantity = finite_float(fields.get("ComponentShare")) or 0.0
            creation_cash = finite_float(fields.get("CreationCashSubstitute"))
            redemption_cash = finite_float(fields.get("RedemptionCashSubstitute"))
            if name == "申赎现金":
                upfront_cash = creation_cash
                continue
            rows.append(
                {
                    "symbol": fund.symbol,
                    "component": identifier,
                    "component_name": name,
                    "market": market_from_source(fields.get("UnderlyingSecurityIDSource", ""), identifier),
                    "quantity": quantity,
                    "substitute_flag": fields.get("SubstituteFlag", ""),
                    "creation_premium_rate": finite_float(fields.get("PremiumRatio")),
                    "redemption_discount_rate": None,
                    "substitution_cash_cny": creation_cash,
                    "redemption_cash_cny": redemption_cash,
                }
            )
        if upfront_cash is None:
            upfront_cash = sum(row["substitution_cash_cny"] or 0.0 for row in rows)
    else:
        unit = float(child_text(root, "CreationRedemptionUnit"))
        nav_per_cu = float(child_text(root, "NAVperCU"))
        estimate_cash = float(child_text(root, "EstimatedCashComponent"))
        switch = child_text(root, "CreationRedemptionSwitch")
        creation_open = redemption_open = switch != "0"
        creation_limit = finite_float(child_text(root, "CreationLimit")) or 0.0
        redemption_limit = finite_float(child_text(root, "RedemptionLimit")) or 0.0
        net_creation_limit = 0.0
        net_redemption_limit = 0.0
        trading_day = child_text(root, "TradingDay")
        for component in root.iter():
            if local_name(component.tag) != "Component":
                continue
            fields = {local_name(child.tag): str(child.text or "").strip() for child in component}
            identifier = fields.get("InstrumentID", "")
            rows.append(
                {
                    "symbol": fund.symbol,
                    "component": identifier,
                    "component_name": fields.get("InstrumentName", ""),
                    "market": market_from_source(fields.get("UnderlyingSecurityID", ""), identifier),
                    "quantity": finite_float(fields.get("Quantity")) or 0.0,
                    "substitute_flag": fields.get("SubstitutionFlag", ""),
                    "creation_premium_rate": finite_float(fields.get("CreationPremiumRate")),
                    "redemption_discount_rate": finite_float(fields.get("RedemptionDiscountRate")),
                    "substitution_cash_cny": finite_float(fields.get("SubstitutionCashAmount")),
                    "redemption_cash_cny": None,
                }
            )
        upfront_cash = sum(row["substitution_cash_cny"] or 0.0 for row in rows) + estimate_cash

    market_counts = pd.Series([row["market"] for row in rows]).value_counts().to_dict()
    summary = {
        "symbol": fund.symbol,
        "name": fund.name,
        "exchange": fund.exchange,
        "pcf_trading_day": trading_day,
        "creation_redemption_unit": unit,
        "nav_per_cu": nav_per_cu,
        "nav_per_share": nav_per_cu / unit,
        "estimate_cash_component_cny": estimate_cash,
        "creation_open": creation_open,
        "redemption_open": redemption_open,
        "creation_limit_shares": creation_limit,
        "redemption_limit_shares": redemption_limit,
        "net_creation_limit_shares": net_creation_limit,
        "net_redemption_limit_shares": net_redemption_limit,
        "component_count": len(rows),
        "market_counts": json.dumps(market_counts, ensure_ascii=False, sort_keys=True),
        "upfront_creation_cash_cny": upfront_cash,
        "upfront_buffer_pct_vs_nav": upfront_cash / nav_per_cu - 1 if nav_per_cu else None,
        "max_creation_premium_rate": max(
            (row["creation_premium_rate"] for row in rows if row["creation_premium_rate"] is not None),
            default=None,
        ),
        "max_redemption_discount_rate": max(
            (row["redemption_discount_rate"] for row in rows if row["redemption_discount_rate"] is not None),
            default=None,
        ),
        "pcf_file": str(path),
        "prospectus_file": str(path.parent / fund.prospectus),
    }
    return summary, rows


class CachedAPI:
    def __init__(self, root: Path, server: str = SERVER, refresh: bool = False) -> None:
        self.root = root
        self.server = server.rstrip("/")
        self.refresh = refresh
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "User-Agent": "NAVNAV-arbitrage-study/1.0"})

    def get(self, cache_name: str, path: str) -> dict[str, Any]:
        cache_path = self.root / cache_name
        if cache_path.is_file() and not self.refresh:
            return json.loads(cache_path.read_text(encoding="utf-8"))
        response = self.session.get(self.server + path, timeout=45)
        response.raise_for_status()
        payload = response.json()
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        time.sleep(0.05)
        return payload


def is_continuous_session(minute: str) -> bool:
    return "09:30" <= minute <= "11:30" or "13:00" <= minute <= "15:00"


def fetch_share_history(api: CachedAPI, fund: Fund) -> pd.DataFrame:
    payload = api.get(f"share/{fund.symbol}.json", f"/api/v1/funds/{fund.symbol}/share-history?days=2000")
    frame = pd.DataFrame(payload.get("rows") or [])
    if frame.empty:
        return frame
    frame["date"] = pd.to_datetime(frame["share_date"])
    frame["symbol"] = fund.symbol
    frame["share_change_10k"] = pd.to_numeric(frame.get("share_change_10k"), errors="coerce").fillna(0.0)
    frame["shares_10k"] = pd.to_numeric(frame["shares_10k"], errors="coerce")
    frame["share_change_units"] = frame["share_change_10k"] / 100.0
    return frame.sort_values("date")


def fetch_public_minutes(api: CachedAPI, fund: Fund) -> pd.DataFrame:
    dates_payload = api.get(
        f"public/{fund.symbol}/dates.json",
        f"/api/v1/funds/{fund.symbol}/minute-history/dates?limit=365",
    )
    frames: list[pd.DataFrame] = []
    for day in dates_payload.get("dates") or []:
        payload = api.get(
            f"public/{fund.symbol}/{day}.json",
            f"/api/v1/funds/{fund.symbol}/minute-history?date={day}",
        )
        frame = pd.DataFrame(payload.get("rows") or [])
        if frame.empty:
            continue
        frame = frame.rename(columns={"min": "minute", "mkp": "market_price", "estnav": "public_estnav", "pmp": "public_premium_pct"})
        frame = frame[frame["minute"].astype(str).map(is_continuous_session)].copy()
        frame["symbol"] = fund.symbol
        frame["date"] = pd.to_datetime(day, format="%Y%m%d")
        for column in ("market_price", "public_estnav", "public_premium_pct"):
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frames.append(frame[["symbol", "date", "minute", "market_price", "public_estnav", "public_premium_pct"]])
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def fetch_private_minutes(api: CachedAPI, fund: Fund) -> pd.DataFrame:
    dates_payload = api.get(
        f"private/{fund.symbol}/dates.json",
        f"/api/v1/private/funds/{fund.symbol}/minute-history/dates",
    )
    frames: list[pd.DataFrame] = []
    for day in dates_payload.get("dates") or []:
        payload = api.get(
            f"private/{fund.symbol}/{day}.json",
            f"/api/v1/private/funds/{fund.symbol}/minute-history?date={day}",
        )
        frame = pd.DataFrame(payload.get("rows") or [])
        if frame.empty:
            continue
        frame["timestamp"] = pd.to_datetime(frame["minute"], utc=True).dt.tz_convert("Asia/Shanghai")
        frame["date"] = frame["timestamp"].dt.tz_localize(None).dt.normalize()
        frame["minute_hm"] = frame["timestamp"].dt.strftime("%H:%M")
        frame["symbol"] = fund.symbol
        numeric = [
            "market_price",
            "basket_bid_nav",
            "basket_ask_nav",
            "buy_direction_premium_rate",
            "sell_direction_premium_rate",
        ]
        for column in numeric:
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
        frames.append(frame)
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def public_daily_screen(public: pd.DataFrame, shares: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    share_lookup = shares.set_index(["symbol", "date"])["share_change_units"] if not shares.empty else pd.Series(dtype=float)
    for (symbol, day), group in public.groupby(["symbol", "date"]):
        group = group.dropna(subset=["market_price", "public_estnav", "public_premium_pct"]).sort_values("minute")
        if group.empty:
            continue
        low = group.loc[group["public_premium_pct"].idxmin()]
        high = group.loc[group["public_premium_pct"].idxmax()]
        discount_price = float(low["market_price"])
        discount_nav = float(low["public_estnav"])
        premium_price = float(high["market_price"])
        premium_nav = float(high["public_estnav"])
        public_create_gross_bps = (premium_price / premium_nav - 1.0) * 10_000
        public_redeem_gross_bps = (discount_nav / discount_price - 1.0) * 10_000
        base = SCENARIOS["base"]
        create_base_net_bps = ((premium_price - TICK_CNY) / premium_nav - 1.0) * 10_000 - base["nonbasket_cost_bps"]
        redeem_base_net_bps = (discount_nav / (discount_price + TICK_CNY) - 1.0) * 10_000 - base["nonbasket_cost_bps"]
        preferred = "create" if create_base_net_bps >= redeem_base_net_bps else "redeem"
        share_units = float(share_lookup.get((symbol, day), 0.0))
        confirmed = (preferred == "create" and share_units > 0) or (preferred == "redeem" and share_units < 0)
        rows.append(
            {
                "symbol": symbol,
                "date": day,
                "rows": len(group),
                "open_premium_pct": float(group.iloc[0]["public_premium_pct"]),
                "close_premium_pct": float(group.iloc[-1]["public_premium_pct"]),
                "median_premium_pct": float(group["public_premium_pct"].median()),
                "min_premium_pct": float(low["public_premium_pct"]),
                "min_premium_minute": low["minute"],
                "max_premium_pct": float(high["public_premium_pct"]),
                "max_premium_minute": high["minute"],
                "public_create_gross_bps": public_create_gross_bps,
                "public_redeem_gross_bps": public_redeem_gross_bps,
                "create_base_net_bps_if_estnav_true": create_base_net_bps,
                "redeem_base_net_bps_if_estnav_true": redeem_base_net_bps,
                "preferred_direction": preferred,
                "preferred_base_net_bps_if_estnav_true": max(create_base_net_bps, redeem_base_net_bps),
                "share_change_units": share_units,
                "directionally_confirmed_flow": confirmed,
                "minutes_discount_50bp": int((group["public_premium_pct"] <= -0.5).sum()),
                "minutes_discount_100bp": int((group["public_premium_pct"] <= -1.0).sum()),
                "minutes_premium_50bp": int((group["public_premium_pct"] >= 0.5).sum()),
                "minutes_premium_100bp": int((group["public_premium_pct"] >= 1.0).sum()),
            }
        )
    return pd.DataFrame(rows)


def private_backtest(private: pd.DataFrame, shares: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if private.empty:
        return private.copy(), pd.DataFrame()
    frame = private.copy()
    for scenario, values in SCENARIOS.items():
        tick_cost = values["adverse_ticks"] * TICK_CNY
        frame[f"create_{scenario}_net_bps"] = (
            (frame["market_price"] - tick_cost) / frame["basket_ask_nav"] - 1.0
        ) * 10_000 - values["nonbasket_cost_bps"]
        frame[f"redeem_{scenario}_net_bps"] = (
            frame["basket_bid_nav"] / (frame["market_price"] + tick_cost) - 1.0
        ) * 10_000 - values["nonbasket_cost_bps"]
    frame["basket_spread_bps"] = (frame["basket_ask_nav"] / frame["basket_bid_nav"] - 1.0) * 10_000
    frame["private_mid_nav"] = (frame["basket_bid_nav"] + frame["basket_ask_nav"]) / 2.0

    share_lookup = shares.set_index(["symbol", "date"])["share_change_units"]
    daily_rows: list[dict[str, Any]] = []
    for (symbol, day), group in frame.groupby(["symbol", "date"]):
        group = group.sort_values("timestamp")
        share_units = float(share_lookup.get((symbol, day), 0.0))
        row: dict[str, Any] = {
            "symbol": symbol,
            "date": day,
            "points": len(group),
            "share_change_units": share_units,
            "median_basket_spread_bps": float(group["basket_spread_bps"].median()),
        }
        for scenario in SCENARIOS:
            create_col = f"create_{scenario}_net_bps"
            redeem_col = f"redeem_{scenario}_net_bps"
            create_best = group.loc[group[create_col].idxmax()]
            redeem_best = group.loc[group[redeem_col].idxmax()]
            row[f"max_create_{scenario}_net_bps"] = float(create_best[create_col])
            row[f"max_create_{scenario}_minute"] = create_best["minute_hm"]
            row[f"max_redeem_{scenario}_net_bps"] = float(redeem_best[redeem_col])
            row[f"max_redeem_{scenario}_minute"] = redeem_best["minute_hm"]
            preferred = "create" if create_best[create_col] >= redeem_best[redeem_col] else "redeem"
            best_value = max(float(create_best[create_col]), float(redeem_best[redeem_col]))
            row[f"preferred_{scenario}"] = preferred
            row[f"best_{scenario}_net_bps"] = best_value
            aligned = (preferred == "create" and share_units > 0) or (preferred == "redeem" and share_units < 0)
            row[f"flow_aligned_{scenario}"] = bool(aligned and best_value > 0)
            nav = float((create_best if preferred == "create" else redeem_best)["market_price"])
            row[f"one_unit_{scenario}_pnl_cny"] = best_value / 10_000 * nav * 1_000_000
            row[f"observed_flow_capacity_{scenario}_pnl_upper_bound_cny"] = (
                row[f"one_unit_{scenario}_pnl_cny"] * abs(share_units) if aligned and best_value > 0 else 0.0
            )
            ordered = group[["timestamp", "minute_hm", "market_price", create_col, redeem_col]].copy()
            ordered["direction"] = np.where(ordered[create_col] >= ordered[redeem_col], "create", "redeem")
            ordered["edge"] = ordered[[create_col, redeem_col]].max(axis=1)
            first_positive = ordered[ordered["edge"] > ENTRY_BUFFER_BPS].head(1)
            if not first_positive.empty:
                first = first_positive.iloc[0]
                row[f"first_positive_{scenario}_minute"] = first["minute_hm"]
                row[f"first_positive_{scenario}_direction"] = first["direction"]
                row[f"first_positive_{scenario}_net_bps"] = float(first["edge"])
            else:
                row[f"first_positive_{scenario}_minute"] = ""
                row[f"first_positive_{scenario}_direction"] = ""
                row[f"first_positive_{scenario}_net_bps"] = None
            ordered["prior_direction"] = ordered["direction"].shift(1)
            ordered["prior_edge"] = ordered["edge"].shift(1)
            confirmed = ordered[
                (ordered["edge"] > ENTRY_BUFFER_BPS)
                & (ordered["prior_edge"] > ENTRY_BUFFER_BPS)
                & (ordered["direction"] == ordered["prior_direction"])
            ].head(1)
            if not confirmed.empty:
                entry = confirmed.iloc[0]
                direction = str(entry["direction"])
                entry_edge = float(entry["edge"])
                entry_aligned = (direction == "create" and share_units > 0) or (direction == "redeem" and share_units < 0)
                row[f"confirmed_entry_{scenario}_minute"] = entry["minute_hm"]
                row[f"confirmed_entry_{scenario}_direction"] = direction
                row[f"confirmed_entry_{scenario}_net_bps"] = entry_edge
                row[f"confirmed_entry_{scenario}_flow_aligned"] = bool(entry_aligned)
                row[f"confirmed_entry_{scenario}_one_unit_pnl_cny"] = (
                    entry_edge / 10_000 * float(entry["market_price"]) * 1_000_000
                )
            else:
                row[f"confirmed_entry_{scenario}_minute"] = ""
                row[f"confirmed_entry_{scenario}_direction"] = ""
                row[f"confirmed_entry_{scenario}_net_bps"] = None
                row[f"confirmed_entry_{scenario}_flow_aligned"] = False
                row[f"confirmed_entry_{scenario}_one_unit_pnl_cny"] = 0.0
        daily_rows.append(row)
    return frame, pd.DataFrame(daily_rows)


def compare_public_private(public: pd.DataFrame, private: pd.DataFrame) -> pd.DataFrame:
    if public.empty or private.empty:
        return pd.DataFrame()
    left = public.rename(columns={"minute": "minute_hm"})
    joined = private.merge(
        left[["symbol", "date", "minute_hm", "public_estnav", "public_premium_pct"]],
        on=["symbol", "date", "minute_hm"],
        how="inner",
        validate="many_to_one",
    )
    joined["public_nav_bias_vs_private_mid_bps"] = (joined["public_estnav"] / joined["private_mid_nav"] - 1.0) * 10_000
    joined["public_premium_vs_private_mid_bps"] = (
        joined["public_premium_pct"] * 100.0
        - (joined["market_price"] / joined["private_mid_nav"] - 1.0) * 10_000
    )
    return joined


def summarize_share_history(shares: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, group in shares.groupby("symbol"):
        group = group.sort_values("date")
        changes = group["share_change_units"]
        nonzero = changes[changes != 0]
        rows.append(
            {
                "symbol": symbol,
                "start_date": group.iloc[0]["date"],
                "end_date": group.iloc[-1]["date"],
                "trading_days": len(group),
                "start_shares_10k": float(group.iloc[0]["shares_10k"]),
                "end_shares_10k": float(group.iloc[-1]["shares_10k"]),
                "net_change_units": float(changes.sum()),
                "creation_days": int((changes > 0).sum()),
                "redemption_days": int((changes < 0).sum()),
                "zero_days": int((changes == 0).sum()),
                "gross_creation_units": float(changes[changes > 0].sum()),
                "gross_redemption_units": float(-changes[changes < 0].sum()),
                "median_abs_nonzero_units": float(nonzero.abs().median()) if len(nonzero) else 0.0,
                "max_creation_units": float(changes.max()),
                "max_redemption_units": float(changes.min()),
                "unit_multiple_failures": int(((changes * 100).round(6) % 100 != 0).sum()),
            }
        )
    return pd.DataFrame(rows)


def kweb_overlap(components: pd.DataFrame) -> pd.DataFrame:
    """Compare current PCF names with KWEB holdings published for 2026-07-09."""
    rows: list[dict[str, Any]] = []
    kweb_names = set(KWEB_HOLDING_WEIGHTS_PCT)
    for symbol, group in components.groupby("symbol"):
        fund_names: set[str] = set()
        for item in group.itertuples(index=False):
            identifier = str(item.component).strip().upper()
            if item.market == "HK":
                identifier = identifier.lstrip("0") or "0"
            fund_names.add(identifier)
        overlap = fund_names & kweb_names
        rows.append(
            {
                "symbol": symbol,
                "fund_component_count": len(fund_names),
                "overlap_count": len(overlap),
                "fund_count_coverage_pct": len(overlap) / len(fund_names) * 100,
                "kweb_weight_represented_pct": sum(KWEB_HOLDING_WEIGHTS_PCT[name] for name in overlap),
                "fund_names_absent_from_kweb": ",".join(sorted(fund_names - kweb_names)),
                "kweb_names_absent_from_fund": ",".join(sorted(kweb_names - fund_names)),
                "kweb_holdings_as_of": "2026-07-09",
                "source": "https://kraneshares.com/etf/kweb/",
            }
        )
    return pd.DataFrame(rows)


def correlation_table(daily: pd.DataFrame) -> pd.DataFrame:
    rows: list[dict[str, Any]] = []
    for symbol, group in daily.groupby("symbol"):
        group = group.sort_values("date").copy()
        group["next_share_change_units"] = group["share_change_units"].shift(-1)
        for feature in ("close_premium_pct", "median_premium_pct", "max_premium_pct", "min_premium_pct"):
            for target in ("share_change_units", "next_share_change_units"):
                valid = group[[feature, target]].dropna()
                rows.append(
                    {
                        "symbol": symbol,
                        "feature": feature,
                        "target": target,
                        "n": len(valid),
                        "pearson": valid[feature].corr(valid[target], method="pearson") if len(valid) >= 3 else None,
                        "spearman": (
                            valid[feature].rank(method="average").corr(
                                valid[target].rank(method="average"), method="pearson"
                            )
                            if len(valid) >= 3
                            else None
                        ),
                    }
                )
    return pd.DataFrame(rows)


def write_data_quality_report(
    path: Path,
    shares: pd.DataFrame,
    public: pd.DataFrame,
    private: pd.DataFrame,
    comparison: pd.DataFrame,
) -> dict[str, Any]:
    duplicate_share = int(shares.duplicated(["symbol", "date"]).sum())
    duplicate_public = int(public.duplicated(["symbol", "date", "minute"]).sum())
    duplicate_private = int(private.duplicated(["symbol", "timestamp"]).sum()) if not private.empty else 0
    report = {
        "as_of": datetime.now().astimezone().isoformat(),
        "share_rows": int(len(shares)),
        "share_duplicate_keys": duplicate_share,
        "share_null_rates": shares[["shares_10k", "share_change_units"]].isna().mean().to_dict(),
        "public_rows": int(len(public)),
        "public_duplicate_keys": duplicate_public,
        "public_date_counts": public.groupby("symbol")["date"].nunique().to_dict(),
        "public_rows_per_day": public.groupby(["symbol", "date"]).size().describe().to_dict(),
        "private_rows": int(len(private)),
        "private_duplicate_keys": duplicate_private,
        "private_date_counts": private.groupby("symbol")["date"].nunique().to_dict() if not private.empty else {},
        "private_rows_per_day": private.groupby(["symbol", "date"]).size().describe().to_dict() if not private.empty else {},
        "public_private_join_rows": int(len(comparison)),
        "known_limitations": [
            "Only SZ159605 has historical private PCF component Bid/Ask rows.",
            "Historical private rows retain public ETF last price, not executable domestic bid/ask.",
            "Public estnav/pmp is a screening estimator and not a two-sided executable basket.",
            "Share changes prove net primary-market activity but not arbitrage motive or participant identity.",
            "Historical borrow availability, market depth, manager execution timestamps, taxes and AP negotiated fees are unavailable.",
        ],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    return report


def make_charts(
    chart_dir: Path,
    shares: pd.DataFrame,
    share_summary: pd.DataFrame,
    daily: pd.DataFrame,
    private_daily: pd.DataFrame,
    comparison: pd.DataFrame,
) -> None:
    chart_dir.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"figure.dpi": 130, "axes.spines.top": False, "axes.spines.right": False})
    colors = {"SZ159605": "#215EA8", "SZ159607": "#D08C19", "SH513050": "#6B7D2A", "SH513220": "#B14A63"}

    fig, axes = plt.subplots(2, 2, figsize=(12, 7.6), sharex=False)
    for axis, fund in zip(axes.ravel(), FUNDS):
        group = shares[shares["symbol"] == fund.symbol].sort_values("date")
        axis.plot(group["date"], group["shares_10k"] / 10_000, color=colors[fund.symbol], linewidth=1.6)
        axis.set_title(f"{fund.symbol} shares outstanding")
        axis.set_ylabel("100m shares")
        axis.grid(axis="y", color="#D9DEE5", linewidth=0.6)
    fig.suptitle("Shares outstanding history by fund", fontsize=14, y=0.99)
    fig.tight_layout(rect=(0, 0, 1, 0.97))
    fig.savefig(chart_dir / "share_history.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)

    chart_source = daily.assign(
        aligned_positive=lambda value: value["directionally_confirmed_flow"]
        & (value["preferred_base_net_bps_if_estnav_true"] > 0)
    )
    screening = chart_source.groupby("symbol").agg(
        days=("date", "nunique"),
        positive_base_screen_days=("preferred_base_net_bps_if_estnav_true", lambda s: int((s > 0).sum())),
        aligned_positive_days=("aligned_positive", "sum"),
    ).reindex([fund.symbol for fund in FUNDS])
    x = np.arange(len(screening))
    fig, axis = plt.subplots(figsize=(10, 5.2))
    width = 0.34
    axis.bar(x - width / 2, screening["positive_base_screen_days"], width, label="Estimator survives base costs", color="#215EA8")
    axis.bar(x + width / 2, screening["aligned_positive_days"], width, label="Direction aligned with net share flow", color="#D08C19")
    axis.set_xticks(x, screening.index)
    axis.set_ylabel("Trading days")
    axis.set_title("Public-estimator candidate days (screen only)")
    axis.legend(frameon=False)
    axis.grid(axis="y", color="#D9DEE5", linewidth=0.6)
    fig.tight_layout()
    fig.savefig(chart_dir / "public_screen_candidates.png", bbox_inches="tight", facecolor="white")
    plt.close(fig)

    if not private_daily.empty:
        group = private_daily.sort_values("date")
        x = np.arange(len(group))
        fig, axis = plt.subplots(figsize=(12, 5.8))
        width = 0.38
        axis.bar(x - width / 2, group["max_create_base_net_bps"], width, label="Create / sell ETF", color="#215EA8")
        axis.bar(x + width / 2, group["max_redeem_base_net_bps"], width, label="Buy ETF / redeem", color="#D08C19")
        axis.axhline(0, color="#333333", linewidth=0.9)
        axis.set_xticks(x, group["date"].dt.strftime("%m-%d"), rotation=45, ha="right")
        axis.set_ylabel("Best net edge (bps)")
        axis.set_title("SZ159605 daily best PCF-basket edge under base cost scenario")
        axis.legend(frameon=False)
        axis.grid(axis="y", color="#D9DEE5", linewidth=0.6)
        fig.tight_layout()
        fig.savefig(chart_dir / "159605_private_edges.png", bbox_inches="tight", facecolor="white")
        plt.close(fig)

    if not comparison.empty:
        values = comparison["public_nav_bias_vs_private_mid_bps"].dropna()
        fig, axis = plt.subplots(figsize=(9.5, 5.2))
        axis.hist(values, bins=min(35, max(10, int(np.sqrt(len(values))))), color="#6B7D2A", edgecolor="white")
        axis.axvline(values.median(), color="#222222", linewidth=1.2, linestyle="--", label=f"Median {values.median():.1f} bps")
        axis.set_xlabel("Public estnav bias vs private basket mid (bps)")
        axis.set_ylabel("Matched observations")
        axis.set_title("SZ159605 public estimator error on matched PCF dates")
        axis.legend(frameon=False)
        axis.grid(axis="y", color="#D9DEE5", linewidth=0.6)
        fig.tight_layout()
        fig.savefig(chart_dir / "159605_public_private_error.png", bbox_inches="tight", facecolor="white")
        plt.close(fig)


def build_summary(
    share_summary: pd.DataFrame,
    daily: pd.DataFrame,
    private_daily: pd.DataFrame,
    comparison: pd.DataFrame,
    pcf_summary: pd.DataFrame,
    kweb: pd.DataFrame,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "generated_at": datetime.now().astimezone().isoformat(),
        "share_summary": share_summary.to_dict(orient="records"),
        "pcf_summary": pcf_summary.to_dict(orient="records"),
        "kweb_overlap": kweb.to_dict(orient="records"),
        "public_screen_by_fund": [],
        "private_159605": {},
        "public_private_error": {},
    }
    for symbol, group in daily.groupby("symbol"):
        result["public_screen_by_fund"].append(
            {
                "symbol": symbol,
                "days": int(group["date"].nunique()),
                "base_positive_screen_days": int((group["preferred_base_net_bps_if_estnav_true"] > 0).sum()),
                "flow_aligned_days": int((group["directionally_confirmed_flow"] & (group["preferred_base_net_bps_if_estnav_true"] > 0)).sum()),
                "max_screen_net_bps": float(group["preferred_base_net_bps_if_estnav_true"].max()),
            }
        )
    if not private_daily.empty:
        result["private_159605"] = {
            "days": int(private_daily["date"].nunique()),
            "base_positive_days": int((private_daily["best_base_net_bps"] > 0).sum()),
            "base_flow_aligned_positive_days": int(private_daily["flow_aligned_base"].sum()),
            "optimistic_flow_aligned_positive_days": int(private_daily["flow_aligned_optimistic"].sum()),
            "stress_flow_aligned_positive_days": int(private_daily["flow_aligned_stress"].sum()),
            "max_base_net_bps": float(private_daily["best_base_net_bps"].max()),
            "median_basket_spread_bps": float(private_daily["median_basket_spread_bps"].median()),
            "observed_flow_capacity_base_pnl_upper_bound_cny": float(
                private_daily["observed_flow_capacity_base_pnl_upper_bound_cny"].sum()
            ),
            "confirmed_entry_base_days": int(private_daily["confirmed_entry_base_net_bps"].notna().sum()),
            "confirmed_entry_base_flow_aligned_days": int(private_daily["confirmed_entry_base_flow_aligned"].sum()),
            "confirmed_entry_stress_flow_aligned_days": int(private_daily["confirmed_entry_stress_flow_aligned"].sum()),
            "confirmed_entry_base_one_unit_pnl_cny": float(
                private_daily.loc[
                    private_daily["confirmed_entry_base_flow_aligned"],
                    "confirmed_entry_base_one_unit_pnl_cny",
                ].sum()
            ),
        }
    if not comparison.empty:
        values = comparison["public_nav_bias_vs_private_mid_bps"].abs().dropna()
        signed = comparison["public_nav_bias_vs_private_mid_bps"].dropna()
        result["public_private_error"] = {
            "matched_rows": int(len(comparison)),
            "signed_median_bps": float(signed.median()),
            "absolute_median_bps": float(values.median()),
            "absolute_p90_bps": float(values.quantile(0.9)),
            "absolute_max_bps": float(values.max()),
        }
    return result


def run_analysis(args: argparse.Namespace) -> int:
    root = Path(args.root).expanduser().resolve()
    raw_dir = root / "data" / "raw"
    processed_dir = root / "data" / "processed"
    chart_dir = root / "charts"
    processed_dir.mkdir(parents=True, exist_ok=True)
    api = CachedAPI(raw_dir, args.server, args.refresh)

    pcf_summaries: list[dict[str, Any]] = []
    pcf_components: list[dict[str, Any]] = []
    for fund in FUNDS:
        summary, rows = parse_pcf_snapshot(root / fund.pcf_file, fund)
        pcf_summaries.append(summary)
        pcf_components.extend(rows)
    pcf_summary = pd.DataFrame(pcf_summaries)
    pcf_component_frame = pd.DataFrame(pcf_components)
    kweb = kweb_overlap(pcf_component_frame)

    shares = pd.concat([fetch_share_history(api, fund) for fund in FUNDS], ignore_index=True)
    public = pd.concat([fetch_public_minutes(api, fund) for fund in FUNDS], ignore_index=True)
    private_frames = [fetch_private_minutes(api, fund) for fund in FUNDS]
    private = pd.concat([frame for frame in private_frames if not frame.empty], ignore_index=True) if any(not frame.empty for frame in private_frames) else pd.DataFrame()

    share_summary = summarize_share_history(shares)
    daily = public_daily_screen(public, shares)
    private_points, private_daily = private_backtest(private, shares)
    comparison = compare_public_private(public, private_points)
    correlations = correlation_table(daily)

    pcf_summary.to_csv(processed_dir / "pcf_summary.csv", index=False)
    pcf_component_frame.to_csv(processed_dir / "pcf_components.csv", index=False)
    kweb.to_csv(processed_dir / "kweb_overlap.csv", index=False)
    shares.to_csv(processed_dir / "share_history.csv", index=False)
    share_summary.to_csv(processed_dir / "share_history_summary.csv", index=False)
    public.to_csv(processed_dir / "public_minute_history.csv", index=False)
    daily.to_csv(processed_dir / "public_daily_screen.csv", index=False)
    private_points.to_csv(processed_dir / "private_minute_backtest.csv", index=False)
    private_daily.to_csv(processed_dir / "private_daily_backtest.csv", index=False)
    comparison.to_csv(processed_dir / "public_private_comparison.csv", index=False)
    correlations.to_csv(processed_dir / "premium_share_flow_correlations.csv", index=False)

    quality = write_data_quality_report(processed_dir / "data_quality_report.json", shares, public, private, comparison)
    make_charts(chart_dir, shares, share_summary, daily, private_daily, comparison)
    summary = build_summary(share_summary, daily, private_daily, comparison, pcf_summary, kweb)
    summary["data_quality"] = quality
    summary = json_safe(summary)
    (processed_dir / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--server", default=SERVER)
    parser.add_argument("--refresh", action="store_true", help="Ignore cached API responses")
    return parser


if __name__ == "__main__":
    raise SystemExit(run_analysis(build_parser().parse_args()))
