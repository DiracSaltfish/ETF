from __future__ import annotations

from pathlib import Path

import pandas as pd

from basket_models import BasketDocument, BasketItem, normalize_action, normalize_symbol


SYMBOL_COLUMNS = {"ticker", "symbol", "证券代码", "股票代码", "代码"}
ACTION_COLUMNS = {"action", "side", "direction", "买卖方向", "操作"}
QTY_COLUMNS = {"quantity", "qty", "shares", "数量", "股数", "目标股数"}
NAME_COLUMNS = {"name", "securityname", "证券名称", "名称"}
TARGET_SHORT_COLUMNS = {"targetshortshares"}


def _normalize_column_name(value: object) -> str:
    text = str(value or "").strip().lower()
    for char in (" ", "_", "-", "/", "\\", "\n", "\t", "（", "）", "(", ")", "%"):
        text = text.replace(char, "")
    return text


def _as_int(value: object) -> int:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return 0
    text = str(value).replace(",", "").strip()
    if not text:
        return 0
    return int(round(float(text)))


def _first_match(columns: dict[str, str], candidates: set[str]) -> str | None:
    for normalized, original in columns.items():
        if normalized in candidates:
            return original
    return None


def _read_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return [(path.stem, pd.read_csv(path))]
    if suffix in {".xlsx", ".xls"}:
        workbook = pd.read_excel(path, sheet_name=None)
        return list(workbook.items())
    raise ValueError(f"暂不支持的文件类型: {path.suffix}")


def _parse_generic_rows(
    sheet_name: str,
    frame: pd.DataFrame,
    *,
    symbol_col: str,
    qty_col: str,
    action_col: str | None,
    name_col: str | None,
    default_action: str,
) -> list[BasketItem]:
    rows: list[BasketItem] = []
    for row_index, row in frame.iterrows():
        symbol = normalize_symbol(row.get(symbol_col))
        qty = _as_int(row.get(qty_col))
        if not symbol or qty == 0:
            continue
        action = normalize_action(row.get(action_col), default=default_action) if action_col else default_action
        if action_col is None and qty < 0:
            action = "SELL"
        name = str(row.get(name_col) or "").strip() if name_col else ""
        rows.append(
            BasketItem(
                symbol=symbol,
                action=action,
                quantity=abs(qty),
                name=name,
                source_sheet=sheet_name,
                source_row=int(row_index) + 2,
            )
        )
    return rows


def _aggregate_rows(rows: list[BasketItem]) -> tuple[BasketItem, ...]:
    grouped: dict[tuple[str, str], BasketItem] = {}
    quantities: dict[tuple[str, str], int] = {}
    for row in rows:
        key = (row.symbol, row.action)
        quantities[key] = quantities.get(key, 0) + row.quantity
        if key not in grouped:
            grouped[key] = row
    result = [
        BasketItem(
            symbol=row.symbol,
            action=row.action,
            quantity=quantities[key],
            name=row.name,
            source_sheet=row.source_sheet,
            source_row=row.source_row,
        )
        for key, row in grouped.items()
    ]
    return tuple(result)


def load_basket_document(path: str | Path) -> BasketDocument:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"篮子文件不存在: {file_path}")

    tables = _read_tables(file_path)
    prioritized = sorted(
        tables,
        key=lambda item: (
            0 if item[0].strip().lower() == "orders" else 1,
            0 if item[0].strip().lower() == "basket" else 1,
            item[0].lower(),
        ),
    )

    last_error = "没有找到可识别的篮子列"
    for sheet_name, frame in prioritized:
        if frame.empty:
            continue
        columns = {_normalize_column_name(col): str(col) for col in frame.columns}
        symbol_col = _first_match(columns, SYMBOL_COLUMNS)
        action_col = _first_match(columns, ACTION_COLUMNS)
        qty_col = _first_match(columns, QTY_COLUMNS)
        name_col = _first_match(columns, NAME_COLUMNS)
        target_short_col = _first_match(columns, TARGET_SHORT_COLUMNS)
        if symbol_col and action_col and qty_col:
            rows = _parse_generic_rows(
                sheet_name,
                frame,
                symbol_col=symbol_col,
                qty_col=qty_col,
                action_col=action_col,
                name_col=name_col,
                default_action="SELL",
            )
        elif symbol_col and target_short_col:
            rows = _parse_generic_rows(
                sheet_name,
                frame,
                symbol_col=symbol_col,
                qty_col=target_short_col,
                action_col=None,
                name_col=name_col,
                default_action="SELL",
            )
        elif symbol_col and qty_col:
            rows = _parse_generic_rows(
                sheet_name,
                frame,
                symbol_col=symbol_col,
                qty_col=qty_col,
                action_col=None,
                name_col=name_col,
                default_action="SELL",
            )
        else:
            last_error = f"工作表 {sheet_name} 没有找到可识别列"
            continue
        rows = [row for row in rows if row.quantity > 0]
        if rows:
            return BasketDocument(
                path=file_path,
                name=file_path.stem,
                rows=_aggregate_rows(rows),
                metadata={"sheet": sheet_name},
            )
        last_error = f"工作表 {sheet_name} 可识别，但没有有效篮子行"

    raise ValueError(last_error)
