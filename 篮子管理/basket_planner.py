from __future__ import annotations

from pathlib import Path

import pandas as pd

from basket_models import BasketDocument, BasketItem


def _normalize_column_name(value: object) -> str:
    text = str(value or "").strip().lower()
    for char in (" ", "_", "-", "/", "\\", "\n", "\t", "（", "）", "(", ")", "%"):
        text = text.replace(char, "")
    return text


def _read_sheet(path: Path, sheet_name: str) -> pd.DataFrame | None:
    try:
        return pd.read_excel(path, sheet_name=sheet_name)
    except ValueError:
        return None


def default_target_xop_shares(basket: BasketDocument, *, base_symbol: str = "XOP") -> int:
    summary = _read_sheet(basket.path, "Summary")
    if summary is not None and {"metric", "value"}.issubset(summary.columns):
        metric_map = {
            str(row.metric).strip(): row.value
            for row in summary.itertuples(index=False)
        }
        value = metric_map.get("target_xop_shares")
        try:
            parsed = int(round(float(value)))
        except (TypeError, ValueError):
            parsed = 0
        if parsed > 0:
            return parsed

    for item in basket.rows:
        if item.symbol.upper() == base_symbol.upper() and item.action == "BUY" and item.quantity > 0:
            return item.quantity
    return 0


def _summary_xop_price(basket: BasketDocument) -> float:
    summary = _read_sheet(basket.path, "Summary")
    if summary is None or not {"metric", "value"}.issubset(summary.columns):
        return 0.0
    metric_map = {
        str(row.metric).strip(): row.value
        for row in summary.itertuples(index=False)
    }
    value = metric_map.get("xop_price")
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = 0.0
    return parsed if parsed > 0 else 0.0


def _load_basket_basis(basket: BasketDocument, *, base_symbol: str = "XOP") -> tuple[tuple[dict[str, object], ...], float]:
    frame = _read_sheet(basket.path, "Basket")
    if frame is None or frame.empty:
        return (), 0.0

    columns = {_normalize_column_name(col): str(col) for col in frame.columns}
    ticker_col = columns.get("ticker")
    weight_col = columns.get("weightpct")
    price_col = columns.get("price")
    name_col = columns.get("name")
    if not ticker_col or not weight_col:
        return (), 0.0

    rows: list[dict[str, object]] = []
    for raw in frame.itertuples(index=False):
        row = raw._asdict()
        symbol = str(row.get(ticker_col) or "").strip().upper()
        if not symbol or symbol == base_symbol.upper():
            continue
        try:
            weight_pct = float(row.get(weight_col) or 0.0)
        except (TypeError, ValueError):
            continue
        try:
            price = float(row.get(price_col) or 0.0) if price_col else 0.0
        except (TypeError, ValueError):
            price = 0.0
        rows.append(
            {
                "symbol": symbol,
                "name": str(row.get(name_col) or "").strip() if name_col else "",
                "weight_pct": weight_pct,
                "reference_price": price,
            }
        )
    total_weight = sum(float(item["weight_pct"]) for item in rows)
    return tuple(rows), total_weight


def build_component_target_basket(
    basket: BasketDocument,
    *,
    target_xop_shares: int,
    base_symbol: str = "XOP",
) -> BasketDocument:
    component_basis, total_weight = _load_basket_basis(basket, base_symbol=base_symbol)
    base_target_xop_shares = default_target_xop_shares(basket, base_symbol=base_symbol)

    if component_basis and total_weight > 0:
        xop_price = _summary_xop_price(basket)
        if xop_price > 0:
            target_nominal_usd = float(target_xop_shares) * xop_price
            rows = []
            for item in component_basis:
                reference_price = float(item["reference_price"] or 0.0)
                if reference_price <= 0:
                    continue
                normalized_weight = float(item["weight_pct"]) / total_weight
                target_shares = int(round(target_nominal_usd * normalized_weight / reference_price))
                if target_shares <= 0:
                    continue
                rows.append(
                    BasketItem(
                        symbol=str(item["symbol"]),
                        action="SELL",
                        quantity=target_shares,
                        name=str(item["name"] or ""),
                        source_sheet="Basket",
                        source_row=0,
                    )
                )
            if rows:
                return BasketDocument(
                    path=basket.path,
                    name=basket.name,
                    rows=tuple(rows),
                    metadata={
                        **basket.metadata,
                        "planner": "basket_sheet_weights",
                        "target_xop_shares": str(target_xop_shares),
                        "base_target_xop_shares": str(base_target_xop_shares),
                    },
                )

    if base_target_xop_shares <= 0:
        component_rows = tuple(item for item in basket.rows if item.symbol.upper() != base_symbol.upper())
        return BasketDocument(
            path=basket.path,
            name=basket.name,
            rows=component_rows,
            metadata={**basket.metadata, "planner": "fixed_component_rows"},
        )

    ratio = float(target_xop_shares) / float(base_target_xop_shares)
    scaled_rows = []
    for item in basket.rows:
        if item.symbol.upper() == base_symbol.upper():
            continue
        quantity = int(round(item.quantity * ratio))
        if quantity <= 0:
            continue
        scaled_rows.append(
            BasketItem(
                symbol=item.symbol,
                action="SELL",
                quantity=quantity,
                name=item.name,
                source_sheet=item.source_sheet,
                source_row=item.source_row,
            )
        )
    return BasketDocument(
        path=basket.path,
        name=basket.name,
        rows=tuple(scaled_rows),
        metadata={
            **basket.metadata,
            "planner": "scaled_fixed_rows",
            "target_xop_shares": str(target_xop_shares),
            "base_target_xop_shares": str(base_target_xop_shares),
        },
    )
