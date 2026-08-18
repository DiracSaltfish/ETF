#!/usr/bin/env python3
"""Read-only IBKR shortability snapshot for current China-internet PCF names.

This is a current feasibility check, not a historical shortability backfill.
It requests generic tick 236 (shortable shares) and never places an order.
"""

from __future__ import annotations

import argparse
import math
from datetime import datetime
from pathlib import Path

import pandas as pd
from ib_insync import Contract, IB, Stock


def positive(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def contract_for_component(market: str, symbol: str) -> Contract:
    market, symbol = market.upper(), symbol.upper()
    if market == "HK":
        return Contract(symbol=symbol.lstrip("0") or "0", secType="STK", exchange="SEHK", currency="HKD")
    if market == "US":
        return Stock(symbol, "SMART", "USD")
    raise ValueError(f"unsupported market {market}")


def fetch_ib_shortability(args: argparse.Namespace) -> pd.DataFrame:
    root = Path(args.root).expanduser().resolve()
    components = pd.read_csv(root / "data" / "processed" / "pcf_components.csv", dtype={"component": str})
    components = components[components["market"].isin(["HK", "US"])].copy()
    components["component"] = components["component"].str.upper().str.strip()
    components.loc[components["market"] == "HK", "component"] = components.loc[
        components["market"] == "HK", "component"
    ].map(lambda value: value.lstrip("0") or "0")
    membership = (
        components.groupby(["market", "component"])["symbol"]
        .apply(lambda values: ",".join(sorted(set(values))))
        .to_dict()
    )
    names = (
        components.groupby(["market", "component"])["component_name"]
        .first()
        .to_dict()
    )
    keys = sorted(membership)
    keys.append(("US", "KWEB"))
    membership[("US", "KWEB")] = "proxy"
    names[("US", "KWEB")] = "KraneShares CSI China Internet ETF"

    ib = IB()
    rows: list[dict[str, object]] = []
    tickers: list[tuple[tuple[str, str], Contract, object]] = []
    try:
        ib.connect(args.host, args.port, clientId=args.client_id, timeout=args.timeout, readonly=True)
        if not ib.isConnected():
            raise RuntimeError("TWS did not become connected")
        ib.reqMarketDataType(args.market_data_type)
        for market, symbol in keys:
            requested = contract_for_component(market, symbol)
            try:
                qualified = ib.qualifyContracts(requested)
            except Exception as exc:
                rows.append(
                    {
                        "market": market,
                        "component": symbol,
                        "component_name": names[(market, symbol)],
                        "funds": membership[(market, symbol)],
                        "qualified": False,
                        "error": str(exc),
                    }
                )
                continue
            if len(qualified) != 1:
                rows.append(
                    {
                        "market": market,
                        "component": symbol,
                        "component_name": names[(market, symbol)],
                        "funds": membership[(market, symbol)],
                        "qualified": False,
                        "error": f"qualified_count={len(qualified)}",
                    }
                )
                continue
            contract = qualified[0]
            ticker = ib.reqMktData(contract, "236", False, False)
            tickers.append(((market, symbol), contract, ticker))
            ib.sleep(args.request_pause)
        ib.sleep(args.wait)
        observed_at = datetime.now().astimezone().isoformat()
        for (market, symbol), contract, ticker in tickers:
            bid = positive(getattr(ticker, "bid", None))
            ask = positive(getattr(ticker, "ask", None))
            shortable_shares = positive(getattr(ticker, "shortableShares", None))
            rows.append(
                {
                    "observed_at": observed_at,
                    "market": market,
                    "component": symbol,
                    "component_name": names[(market, symbol)],
                    "funds": membership[(market, symbol)],
                    "qualified": True,
                    "con_id": contract.conId,
                    "local_symbol": contract.localSymbol,
                    "primary_exchange": contract.primaryExchange,
                    "market_data_type": getattr(ticker, "marketDataType", None),
                    "bid": bid,
                    "ask": ask,
                    "spread_bps": (ask / bid - 1.0) * 10_000 if bid and ask and ask >= bid else None,
                    "shortable_shares": shortable_shares,
                    "shortability_status": "positive" if shortable_shares else "unknown_or_zero",
                    "error": "",
                }
            )
            ib.cancelMktData(contract)
    finally:
        if ib.isConnected():
            ib.disconnect()
    frame = pd.DataFrame(rows).sort_values(["market", "component"])
    output = root / "data" / "processed" / "ib_shortability_snapshot.csv"
    frame.to_csv(output, index=False)
    return frame


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parent))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7496)
    parser.add_argument("--client-id", type=int, default=513229)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--wait", type=float, default=8.0)
    parser.add_argument("--request-pause", type=float, default=0.05)
    parser.add_argument("--market-data-type", type=int, default=2, choices=[1, 2, 3, 4])
    return parser


if __name__ == "__main__":
    result = fetch_ib_shortability(build_parser().parse_args())
    print(result.to_string(index=False))
