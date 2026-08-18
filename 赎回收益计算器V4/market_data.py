from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


@dataclass(frozen=True)
class XopDailyPrice:
    symbol: str
    trade_day: date
    close: Decimal
    vwap_1540_1550: Decimal | None = None
    vwap_1540_1600: Decimal | None = None
    vwap_1554_1557: Decimal | None = None
    last_1559: Decimal | None = None
    last_1600: Decimal | None = None
    source: str = "manual_csv"


class XopPriceProvider:
    def get_close(self, trade_day: date) -> Decimal:
        raise NotImplementedError

    def get_window_price(self, trade_day: date, window: str) -> Decimal:
        raise NotImplementedError


class CsvXopPriceProvider(XopPriceProvider):
    """Local, deterministic XOP prices keyed by US trade date."""

    def __init__(self, csv_path: Path | str) -> None:
        self.csv_path = Path(csv_path).expanduser().resolve()

    @staticmethod
    def _decimal(value: object, field: str, *, required: bool = False) -> Decimal | None:
        text = str(value or "").replace(",", "").strip()
        if not text:
            if required:
                raise ValueError(f"XOP CSV 缺少 {field}")
            return None
        try:
            parsed = Decimal(text)
        except InvalidOperation as exc:
            raise ValueError(f"XOP CSV {field} 不是有效数字: {text}") from exc
        if parsed <= 0:
            raise ValueError(f"XOP CSV {field} 必须大于 0: {text}")
        return parsed

    def load_prices(self) -> list[XopDailyPrice]:
        if not self.csv_path.exists():
            return []
        prices: list[XopDailyPrice] = []
        with self.csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
            for row_number, row in enumerate(csv.DictReader(handle), start=2):
                symbol = str(row.get("symbol") or "XOP").strip().upper()
                if symbol != "XOP":
                    continue
                try:
                    trade_day = date.fromisoformat(str(row.get("trade_day") or "").strip())
                    close = self._decimal(row.get("close"), "close", required=True)
                    assert close is not None
                    prices.append(
                        XopDailyPrice(
                            symbol=symbol,
                            trade_day=trade_day,
                            close=close,
                            vwap_1540_1550=self._decimal(row.get("vwap_1540_1550"), "vwap_1540_1550"),
                            vwap_1540_1600=self._decimal(row.get("vwap_1540_1600"), "vwap_1540_1600"),
                            vwap_1554_1557=self._decimal(row.get("vwap_1554_1557"), "vwap_1554_1557"),
                            last_1559=self._decimal(row.get("last_1559"), "last_1559"),
                            last_1600=self._decimal(row.get("last_1600"), "last_1600"),
                            source=str(row.get("source") or "manual_csv").strip(),
                        )
                    )
                except ValueError as exc:
                    raise ValueError(f"{self.csv_path.name} 第 {row_number} 行: {exc}") from exc
        prices.sort(key=lambda item: item.trade_day)
        return prices

    def get_daily_price(self, trade_day: date) -> XopDailyPrice:
        matched = [item for item in self.load_prices() if item.trade_day == trade_day]
        if not matched:
            raise KeyError(f"XOP CSV 中没有 {trade_day.isoformat()} 的价格")
        return matched[-1]

    def get_close(self, trade_day: date) -> Decimal:
        return self.get_daily_price(trade_day).close

    def get_window_price(self, trade_day: date, window: str) -> Decimal:
        price = self.get_daily_price(trade_day)
        if window == "1540_1550":
            candidates = (price.vwap_1540_1550, price.vwap_1540_1600, price.last_1600, price.close)
        elif window == "1540_1600":
            candidates = (price.vwap_1540_1600, price.last_1600, price.close)
        elif window == "1554_1557":
            candidates = (price.vwap_1554_1557, price.last_1600, price.close)
        elif window == "1559_close":
            candidates = (price.last_1559, price.last_1600, price.close)
        else:
            raise ValueError(f"不支持的 XOP 价格窗口: {window}")
        return next(item for item in candidates if item is not None)


def upsert_xop_prices(csv_path: Path | str, prices: list[XopDailyPrice]) -> None:
    """Merge prices by (symbol, trade_day), preserving unrelated existing rows."""
    path = Path(csv_path).expanduser().resolve()
    existing = CsvXopPriceProvider(path).load_prices()
    merged = {(item.symbol, item.trade_day): item for item in existing}
    merged.update({(item.symbol, item.trade_day): item for item in prices})
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "symbol", "trade_day", "close", "vwap_1540_1550", "vwap_1540_1600",
        "vwap_1554_1557", "last_1559", "last_1600", "source"
    )
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields))
        writer.writeheader()
        for item in sorted(merged.values(), key=lambda value: (value.trade_day, value.symbol)):
            writer.writerow(
                {
                    "symbol": item.symbol,
                    "trade_day": item.trade_day.isoformat(),
                    "close": item.close,
                    "vwap_1540_1550": item.vwap_1540_1550 or "",
                    "vwap_1540_1600": item.vwap_1540_1600 or "",
                    "vwap_1554_1557": item.vwap_1554_1557 or "",
                    "last_1559": item.last_1559 or "",
                    "last_1600": item.last_1600 or "",
                    "source": item.source,
                }
            )
