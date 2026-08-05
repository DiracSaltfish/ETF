from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterable

import pandas as pd

from india_models import (
    IbFill,
    IndiaTrade,
    PositionSnapshot,
    RedemptionEvent,
    StatementImportIssue,
    StatementImportResult,
)


def decimal_value(value: object) -> Decimal:
    if value is None or value == "" or (isinstance(value, float) and pd.isna(value)):
        return Decimal("0")
    try:
        return Decimal(str(value).replace(",", "").replace("￥", "").strip())
    except (InvalidOperation, ValueError):
        return Decimal("0")


def normalize_code(value: object) -> str:
    text = str(value or "").strip().upper().replace("SZ", "").replace("SH", "")
    if text.endswith(".0"):
        text = text[:-2]
    if "." in text:
        text = text.split(".", 1)[0]
    return text.zfill(6) if text.isdigit() else text


def parse_day(value: object) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip().replace("/", "-")
    if not text:
        return None
    for pattern in ("%Y-%m-%d", "%Y%m%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:19], pattern).date()
        except ValueError:
            continue
    return None


def parse_datetime(day_value: object, time_value: object = None) -> datetime | None:
    if isinstance(day_value, datetime):
        return day_value
    day = parse_day(day_value)
    if day is None:
        return None
    text = str(time_value or "").strip()
    if not text:
        return None
    for pattern in ("%H:%M:%S", "%H:%M", "%H%M%S"):
        try:
            parsed = datetime.strptime(text.split(".", 1)[0].zfill(6), pattern).time()
            return datetime.combine(day, parsed)
        except ValueError:
            continue
    return datetime.combine(day, datetime.min.time())


def _is_text_table(path: Path) -> bool:
    with path.open("rb") as handle:
        header = handle.read(8192)
    return not header.startswith((b"PK\x03\x04", b"\xd0\xcf\x11\xe0")) and b"\t" in header


def read_table(path: Path | str) -> pd.DataFrame:
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        raise FileNotFoundError(f"文件不存在：{file_path}")
    if file_path.suffix.lower() in {".csv"}:
        frame = pd.read_csv(file_path, encoding="utf-8-sig", dtype=object, keep_default_na=False)
    elif file_path.suffix.lower() in {".xlsx", ".xlsm"} or not _is_text_table(file_path):
        frame = pd.read_excel(file_path, dtype=object)
    else:
        errors: list[str] = []
        for encoding in ("utf-8-sig", "gb18030", "utf-16"):
            try:
                frame = pd.read_csv(file_path, sep="\t", encoding=encoding, dtype=object, keep_default_na=False)
                break
            except UnicodeError as exc:
                errors.append(f"{encoding}: {exc}")
        else:
            raise ValueError("表格编码无法识别：" + " | ".join(errors))
    frame.columns = [str(item).replace("\ufeff", "").strip() for item in frame.columns]
    return frame


FIELD_ALIASES = {
    "code": ("证券代码", "证券代码(原始)", "代码", "基金代码", "标的代码", "code", "stock_code", "security_code"),
    "day": ("交易日期", "交易日", "成交日期", "业务日期", "日期"),
    "redeem_day": ("赎回日期", "申请日期", "交易日期", "交易日", "业务日期", "日期"),
    "action": ("业务名称", "业务类型", "操作", "买卖方向", "方向", "交易类型"),
    "qty": ("成交数量", "数量", "发生数量", "证券数量", "份额"),
    "price": ("成交价格", "价格", "成交均价", "单价"),
    "amount": ("发生金额", "成交金额", "金额", "发生金额(元)", "清算金额"),
    "contract": ("合同号", "合同编号", "委托号", "成交编号", "业务流水号"),
    "name": ("证券名称", "基金名称", "名称"),
    "time": ("成交时间", "成交时刻", "委托时间", "时间"),
    "account": ("账户", "资金账号", "证券账户", "QMT窗口", "窗口", "来源"),
    "gross": ("赎回金额", "毛赎回款", "毛额", "应收金额"),
    "fee": ("赎回费", "手续费", "费用", "基金赎回费"),
    "net": ("净赎回款", "到账金额", "实收金额", "净额", "净发生金额"),
    "nav": ("净值", "单位净值", "赎回净值", "基金净值"),
    "statement_day": ("交割单日期", "交收日期", "到账日期", "资金到账日"),
}


def _column(frame: pd.DataFrame, field: str) -> str | None:
    for alias in FIELD_ALIASES[field]:
        if alias in frame.columns:
            return alias
    normalized = {str(col).replace(" ", "").lower(): str(col) for col in frame.columns}
    for alias in FIELD_ALIASES[field]:
        key = alias.replace(" ", "").lower()
        if key in normalized:
            return normalized[key]
    return None


def _value(row: pd.Series, frame: pd.DataFrame, field: str) -> object:
    column = _column(frame, field)
    return row[column] if column else ""


def normalize_action(value: object) -> str | None:
    text = str(value or "").strip().lower()
    if not text:
        return None
    if any(token in text for token in ("赎回", "redemption", "redeem")):
        return "REDEEM"
    if any(token in text for token in ("买入", "申购", "buy", "purchase")):
        return "BUY"
    if any(token in text for token in ("卖出", "sell")):
        return "SELL"
    return None


def load_qmt_file(path: Path | str | None, account: str, fund_code: str = "164824") -> list[IndiaTrade]:
    if path is None or not str(path).strip():
        return []
    frame = read_table(path)
    records: list[IndiaTrade] = []
    for row_number, row in frame.iterrows():
        if normalize_code(_value(row, frame, "code")) != normalize_code(fund_code):
            continue
        action = normalize_action(_value(row, frame, "action"))
        day = parse_day(_value(row, frame, "day"))
        if action is None or day is None:
            continue
        qty = abs(int(decimal_value(_value(row, frame, "qty"))))
        if qty <= 0:
            continue
        price = decimal_value(_value(row, frame, "price"))
        amount = abs(decimal_value(_value(row, frame, "amount")))
        if amount == 0 and price:
            amount = price * qty
        records.append(
            IndiaTrade(
                account=account.upper(),
                row_number=int(row_number) + 2,
                trade_day=day,
                action=action,  # type: ignore[arg-type]
                qty=qty,
                price=price,
                amount=amount,
                contract_no=str(_value(row, frame, "contract") or "").strip(),
                code=normalize_code(fund_code),
                name=str(_value(row, frame, "name") or "").strip(),
                trade_dt=parse_datetime(_value(row, frame, "day"), _value(row, frame, "time")),
            )
        )
    records.sort(key=lambda item: (item.event_dt, item.row_number))
    return records


def _contract_candidates(value: str) -> tuple[str, ...]:
    text = str(value or "").strip()
    values = [text]
    if text.isdigit():
        number = int(text)
        values.extend((str(number % 1_000_000), str(number - 3_800_000_000)))
    return tuple(dict.fromkeys(item for item in values if item))


def _load_qmt_time_hints(root: Path | str | None, days: set[date]) -> dict[tuple[str, date, str, int, str], datetime]:
    if not root or not str(root).strip():
        return {}
    root_path = Path(root).expanduser()
    candidates = [root_path / "QMT成交时间.csv"]
    candidates.extend(root_path / f"{day:%Y%m%d}" / "QMT成交时间.csv" for day in sorted(days))
    hints: dict[tuple[str, date, str, int, str], datetime] = {}
    for path in candidates:
        if not path.exists():
            continue
        try:
            handle = path.open("r", encoding="utf-8-sig", newline="")
        except OSError:
            continue
        with handle:
            for row in csv.DictReader(handle):
                if normalize_code(row.get("代码") or row.get("证券代码")) != "164824":
                    continue
                account = str(row.get("QMT窗口") or row.get("窗口") or row.get("来源") or "").strip().upper()
                if account not in {"QMT1", "QMT2", "QMT3"}:
                    continue
                day = parse_day(row.get("交易日") or row.get("处理日期") or row.get("委托日期"))
                if day is None or (days and day not in days):
                    continue
                action = normalize_action(row.get("方向") or row.get("买卖方向"))
                if action not in {"BUY", "SELL"}:
                    continue
                qty = abs(int(decimal_value(row.get("成交数量") or row.get("数量"))))
                contract = str(row.get("委托号") or row.get("合同号") or row.get("合同编号") or "").strip()
                timestamp = parse_datetime(
                    row.get("处理日期") or row.get("交易日") or row.get("委托日期"),
                    row.get("成交处理时间") or row.get("记录时间") or row.get("委托时间"),
                )
                if timestamp is None or qty <= 0:
                    continue
                for candidate in _contract_candidates(contract):
                    hints.setdefault((account, day, action, qty, candidate), timestamp)
    return hints


def load_qmt_accounts(
    paths: dict[str, Path | str | None],
    fund_code: str = "164824",
    time_root: Path | str | None = None,
) -> list[IndiaTrade]:
    records: list[IndiaTrade] = []
    for account in ("QMT1", "QMT2", "QMT3"):
        records.extend(load_qmt_file(paths.get(account), account, fund_code))
    hints = _load_qmt_time_hints(time_root, {item.trade_day for item in records})
    enriched: list[IndiaTrade] = []
    for item in records:
        if item.trade_dt is None and item.action in {"BUY", "SELL"}:
            for candidate in _contract_candidates(item.contract_no):
                hint = hints.get((item.account, item.trade_day, item.action, item.qty, candidate))
                if hint is not None:
                    item = replace(item, trade_dt=hint)
                    break
        enriched.append(item)
    return sorted(enriched, key=lambda item: (item.event_dt, item.account, item.row_number))


DATE_DIRECTORY_RE = re.compile(r"^\d{8}$")
POSITION_QUANTITY_COLUMNS = ("volume", "qty", "position", "current_amount", "持仓数量", "数量")
POSITION_AVAILABLE_COLUMNS = ("available", "can_use_volume", "enable_amount", "可用数量")


def _column_by_aliases(frame: pd.DataFrame, aliases: Iterable[str]) -> str | None:
    normalized = {str(column).replace(" ", "").lower(): str(column) for column in frame.columns}
    for alias in aliases:
        if alias in frame.columns:
            return alias
        candidate = normalized.get(alias.replace(" ", "").lower())
        if candidate:
            return candidate
    return None


def load_position_snapshots(
    root: Path | str | None,
    fund_code: str = "164824",
) -> tuple[PositionSnapshot, ...]:
    """Read dated chicang1/2/3.csv files without modifying the source directory."""
    if root is None or not str(root).strip():
        return ()
    base = Path(root).expanduser().resolve()
    if not base.exists():
        raise FileNotFoundError(f"持仓根目录不存在：{base}")
    snapshots: list[PositionSnapshot] = []
    for day_dir in sorted((item for item in base.iterdir() if item.is_dir()), key=lambda item: item.name):
        if not DATE_DIRECTORY_RE.fullmatch(day_dir.name):
            continue
        try:
            snapshot_day = datetime.strptime(day_dir.name, "%Y%m%d").date()
        except ValueError:
            continue
        for number, account in enumerate(("QMT1", "QMT2", "QMT3"), start=1):
            path = day_dir / f"chicang{number}.csv"
            if not path.exists():
                continue
            frame = read_table(path)
            code_column = _column(frame, "code")
            qty_column = _column_by_aliases(frame, POSITION_QUANTITY_COLUMNS)
            available_column = _column_by_aliases(frame, POSITION_AVAILABLE_COLUMNS)
            if code_column is None or qty_column is None:
                raise ValueError(f"持仓文件缺少证券代码或持仓数量列：{path}")
            matched = frame[frame[code_column].map(normalize_code) == normalize_code(fund_code)]
            total_qty = sum(abs(int(decimal_value(value))) for value in matched[qty_column].tolist())
            if available_column is None:
                available_qty = total_qty
            else:
                available_qty = sum(abs(int(decimal_value(value))) for value in matched[available_column].tolist())
            snapshots.append(
                PositionSnapshot(
                    day=snapshot_day,
                    account=account,
                    total_qty=total_qty,
                    available_qty=available_qty,
                    source_path=str(path),
                )
            )
    return tuple(sorted(snapshots, key=lambda item: (item.day, item.account)))


def load_redemption_statement(
    path: Path | str,
    fund_code: str = "164824",
    default_account: str = "QMT1",
) -> StatementImportResult:
    frame = read_table(path)
    events: list[RedemptionEvent] = []
    issues: list[StatementImportIssue] = []
    for row_number, row in frame.iterrows():
        raw = " | ".join(str(value) for value in row.tolist())
        code = normalize_code(_value(row, frame, "code"))
        action = normalize_action(_value(row, frame, "action"))
        if code and code != normalize_code(fund_code):
            continue
        if action != "REDEEM":
            continue
        day = parse_day(_value(row, frame, "redeem_day"))
        qty = abs(int(decimal_value(_value(row, frame, "qty"))))
        if day is None or qty <= 0:
            issues.append(StatementImportIssue(int(row_number) + 2, "缺少有效赎回日期或数量", raw))
            continue
        account = str(_value(row, frame, "account") or default_account).strip().upper()
        if account not in {"QMT1", "QMT2", "QMT3"}:
            account = default_account.upper()
        gross = decimal_value(_value(row, frame, "gross")) or None
        fee = decimal_value(_value(row, frame, "fee")) or None
        net = decimal_value(_value(row, frame, "net")) or None
        nav = decimal_value(_value(row, frame, "nav")) or None
        ref = str(_value(row, frame, "contract") or f"row-{int(row_number) + 2}").strip()
        event_id = hashlib.sha1(f"{Path(path).resolve()}|{row_number}|{raw}".encode("utf-8")).hexdigest()[:20]
        statement_day = parse_day(_value(row, frame, "statement_day"))
        events.append(
            RedemptionEvent(
                event_id=f"statement:{event_id}",
                account=account,
                redeem_day=day,
                qty=qty,
                source="statement",
                contract_no=ref,
                gross_amount=gross,
                fee_amount=fee,
                net_amount=net,
                nav_per_share=nav,
                event_dt=datetime.combine(day, datetime.max.time()),
                statement_day=statement_day,
                raw_reference=raw,
            )
        )
    return StatementImportResult(tuple(events), tuple(issues))


def _parse_ib_dt(value: object) -> datetime | None:
    text = str(value or "").strip().replace(" UTC", "")
    for pattern in ("%Y-%m-%d, %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y%m%d;%H:%M:%S"):
        try:
            return datetime.strptime(text, pattern)
        except ValueError:
            continue
    return None


def _header_value(raw: list[str], header: list[str], aliases: Iterable[str]) -> str:
    normalized_aliases = {str(item).strip().lower() for item in aliases}
    for index, name in enumerate(header):
        if str(name).strip().lower() in normalized_aliases and index < len(raw):
            return raw[index].strip()
    return ""


def load_ib_india_fills(path: Path | str | None) -> list[IbFill]:
    if path is None or not str(path).strip():
        return []
    file_path = Path(path).expanduser().resolve()
    if not file_path.exists():
        return []
    fills: list[IbFill] = []
    header: list[str] = []
    with file_path.open("r", encoding="utf-8-sig", newline="") as handle:
        for row_number, raw in enumerate(csv.reader(handle), start=1):
            if len(raw) >= 2 and raw[0] == "交易" and raw[1] == "Header":
                header = raw
                continue
            if len(raw) < 3 or raw[0] != "交易" or raw[1] != "Data" or raw[2] != "Order" or not header:
                continue
            asset_class = _header_value(raw, header, ("资产分类", "Asset Class"))
            symbol = _header_value(raw, header, ("代码", "Symbol")).upper()
            if symbol != "INDA" and not symbol.startswith("NIFTY"):
                continue
            dt = _parse_ib_dt(_header_value(raw, header, ("日期/时间", "Date/Time")))
            if dt is None:
                continue
            quantity = int(decimal_value(_header_value(raw, header, ("数量", "Quantity"))))
            if quantity == 0:
                continue
            price = decimal_value(_header_value(raw, header, ("交易价格", "Trade Price")))
            commission = abs(decimal_value(_header_value(raw, header, ("佣金/税", "佣金 USD", "Commission", "Commission USD"))))
            currency = _header_value(raw, header, ("货币", "Currency")) or "USD"
            order_ref = _header_value(
                raw,
                header,
                ("订单参考", "订单引用", "订单Ref", "Order Reference", "OrderRef"),
            )
            signature = "|".join(raw[:16])
            fill_id = hashlib.sha1(f"{signature}|{row_number}".encode("utf-8")).hexdigest()[:20]
            fills.append(
                IbFill(
                    fill_id=fill_id,
                    symbol=symbol,
                    asset_class=asset_class,
                    dt=dt,
                    qty=quantity,
                    price=price,
                    commission=commission,
                    currency=currency,
                    order_ref=order_ref,
                )
            )
    return sorted(fills, key=lambda item: (item.dt, item.fill_id))
