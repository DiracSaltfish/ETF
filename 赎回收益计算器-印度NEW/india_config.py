from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path


DEFAULT_CONFIG = {
    "fund_code": "164824",
    "fund_name": "工银瑞信印度市场证券投资基金",
    "qmt1_path": "",
    "qmt2_path": "",
    "qmt3_path": "",
    "position_root": "/Users/ellis/Desktop/交易表格",
    "redemption_statement_path": "",
    "ib_path": "",
    "data_root": "",
    "qmt_time_root": "",
    "fx_rate": "6.800000",
    "redemption_fee_rate": "0.00464",
    "redemption_fee_confirmed": False,
    "basket_fund_qty": 270000,
    "nifty_contracts_per_basket": 1,
    "inda_shares_per_basket": 970,
    "inda_first_close_shares": 364,
    "inda_second_close_shares": 606,
    "redemption_holding_days": 3,
    "settlement_statement_days": 5,
    "settlement_available_days": 6,
    "nifty_roll_weekday": 1,
    "nifty_roll_time_zone": "Asia/Shanghai",
    "swap_time_et": "09:40",
    "inda_first_close_time_et": "11:30",
    "inda_second_close_time_et": "15:59",
    "live_enabled": False,
    "china_calendar_years": [2025, 2026],
    "china_market_holidays": [],
    "fund_closed_days": [],
    "tws_host": "127.0.0.1",
    "tws_port": 7496,
    "tws_client_id": 8888,
    "tws_account": "",
    "tws_auto_client_id": True,
}


@dataclass(frozen=True)
class IndiaConfig:
    fund_code: str = "164824"
    fund_name: str = "工银瑞信印度市场证券投资基金"
    basket_fund_qty: int = 270000
    nifty_contracts_per_basket: int = 1
    inda_shares_per_basket: int = 970
    inda_first_close_shares: int = 364
    inda_second_close_shares: int = 606
    redemption_holding_days: int = 3
    settlement_statement_days: int = 5
    settlement_available_days: int = 6
    redemption_fee_rate: Decimal = Decimal("0.00464")
    redemption_fee_confirmed: bool = False
    swap_time_et: str = "09:40"
    inda_first_close_time_et: str = "11:30"
    inda_second_close_time_et: str = "15:59"
    nifty_roll_weekday: int = 1
    nifty_roll_time_zone: str = "Asia/Shanghai"
    live_enabled: bool = False

    def validate(self) -> None:
        positive = {
            "basket_fund_qty": self.basket_fund_qty,
            "nifty_contracts_per_basket": self.nifty_contracts_per_basket,
            "inda_shares_per_basket": self.inda_shares_per_basket,
            "inda_first_close_shares": self.inda_first_close_shares,
            "inda_second_close_shares": self.inda_second_close_shares,
            "redemption_holding_days": self.redemption_holding_days,
            "settlement_statement_days": self.settlement_statement_days,
            "settlement_available_days": self.settlement_available_days,
        }
        for name, value in positive.items():
            if value <= 0:
                raise ValueError(f"{name} 必须大于 0")
        if self.inda_first_close_shares + self.inda_second_close_shares != self.inda_shares_per_basket:
            raise ValueError("INDA 两段平仓数量必须合计等于每篮目标数量")
        if not Decimal("0") <= self.redemption_fee_rate < Decimal("1"):
            raise ValueError("赎回费率必须位于 0 到 1 之间")
        if self.nifty_roll_weekday not in range(7):
            raise ValueError("nifty_roll_weekday 必须位于 0 到 6")

    @classmethod
    def from_mapping(cls, mapping: dict[str, object]) -> "IndiaConfig":
        values = dict(DEFAULT_CONFIG)
        values.update(mapping)
        result = cls(
            fund_code=str(values["fund_code"] or "164824"),
            fund_name=str(values["fund_name"] or DEFAULT_CONFIG["fund_name"]),
            basket_fund_qty=int(values["basket_fund_qty"]),
            nifty_contracts_per_basket=int(values["nifty_contracts_per_basket"]),
            inda_shares_per_basket=int(values["inda_shares_per_basket"]),
            inda_first_close_shares=int(values["inda_first_close_shares"]),
            inda_second_close_shares=int(values["inda_second_close_shares"]),
            redemption_holding_days=int(values["redemption_holding_days"]),
            settlement_statement_days=int(values["settlement_statement_days"]),
            settlement_available_days=int(values["settlement_available_days"]),
            redemption_fee_rate=Decimal(str(values["redemption_fee_rate"])),
            redemption_fee_confirmed=bool(values.get("redemption_fee_confirmed", False)),
            swap_time_et=str(values.get("swap_time_et") or "09:40"),
            inda_first_close_time_et=str(values.get("inda_first_close_time_et") or "11:30"),
            inda_second_close_time_et=str(values.get("inda_second_close_time_et") or "15:59"),
            nifty_roll_weekday=int(values.get("nifty_roll_weekday", 1)),
            nifty_roll_time_zone=str(values.get("nifty_roll_time_zone") or "Asia/Shanghai"),
            live_enabled=bool(values.get("live_enabled", False)),
        )
        result.validate()
        return result


def load_json_config(path: Path | str) -> dict[str, object]:
    config_path = Path(path).expanduser()
    values: dict[str, object] = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            values = raw
    merged = dict(DEFAULT_CONFIG)
    merged.update(values)
    return merged


def load_india_config(path: Path | str) -> IndiaConfig:
    return IndiaConfig.from_mapping(load_json_config(path))


def save_json_config(path: Path | str, values: dict[str, object]) -> None:
    config_path = Path(path).expanduser()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    merged = dict(DEFAULT_CONFIG)
    merged.update(values)
    temporary = config_path.with_suffix(config_path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    temporary.replace(config_path)


def config_mapping(config: IndiaConfig) -> dict[str, object]:
    values = asdict(config)
    values["redemption_fee_rate"] = str(config.redemption_fee_rate)
    return values
