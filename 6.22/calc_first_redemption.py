from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

import pandas as pd


ROOT = Path("/Users/ellis/Desktop/ETF交割/6.22")
QMT_PATH = ROOT / "qmt1.xlsx"
IB_PATH = ROOT / "U15286908_20260601_20260629.csv"
TARGET_CODE = 159518
TARGET_HEDGE_SHARES = 990
FX_RATE = 6.79635  # 2026-06-29 USD.CNH from the same IB statement.


@dataclass
class Lot:
    qty: float
    amount: float
    trade_date: int
    contract_no: int


def load_qmt() -> pd.DataFrame:
    df = pd.read_excel(QMT_PATH)
    df = df[df["证券代码"] == TARGET_CODE].copy()
    df["合同编号"] = df["合同编号"].astype("Int64")
    df["交收日期"] = df["交收日期"].astype(int)
    return df.sort_values(["交收日期", "合同编号"], kind="stable").reset_index(drop=True)


def fifo_consume(lots: list[Lot], qty: float) -> tuple[float, list[dict[str, float]]]:
    remaining = float(qty)
    cost = 0.0
    matches: list[dict[str, float]] = []

    while remaining > 1e-9:
        if not lots:
            raise ValueError(f"Not enough inventory to consume {qty}")
        lot = lots[0]
        used = min(lot.qty, remaining)
        used_amount = lot.amount * used / lot.qty
        cost += used_amount
        matches.append(
            {
                "trade_date": lot.trade_date,
                "contract_no": lot.contract_no,
                "used_qty": used,
                "used_amount": used_amount,
            }
        )
        lot.qty -= used
        lot.amount -= used_amount
        remaining -= used
        if lot.qty <= 1e-9:
            lots.pop(0)

    return cost, matches


def analyze_qmt() -> dict:
    df = load_qmt()
    lots: list[Lot] = []
    sell_records = []

    redemption_row = (
        df[df["操作"] == "ETF 基金赎回"]
        .sort_values(["交收日期", "合同编号"], kind="stable")
        .iloc[0]
    )
    redemption_contract = int(redemption_row["合同编号"])
    redemption_date = int(redemption_row["交收日期"])
    redemption_qty = float(redemption_row["成交数量"])

    redemption_cost = None
    redemption_matches = None

    for row in df.itertuples(index=False):
        action = row.操作
        qty = float(row.成交数量)
        amount = abs(float(row.发生金额))
        contract_no = int(row.合同编号)
        trade_date = int(row.交收日期)

        if action == "证券买入":
            lots.append(Lot(qty=qty, amount=amount, trade_date=trade_date, contract_no=contract_no))
            continue

        if action == "证券卖出":
            sell_cost, sell_matches = fifo_consume(lots, qty)
            sell_records.append(
                {
                    "trade_date": trade_date,
                    "contract_no": contract_no,
                    "qty": qty,
                    "net_amount": float(row.发生金额),
                    "cost": sell_cost,
                    "pnl": float(row.发生金额) - sell_cost,
                    "matches": sell_matches,
                }
            )
            continue

        if action == "ETF 基金赎回" and contract_no == redemption_contract:
            redemption_cost, redemption_matches = fifo_consume(lots, qty)
            break

    if redemption_cost is None or redemption_matches is None:
        raise ValueError("Failed to match the first redemption basket")

    related_rows = df[df["合同编号"] == redemption_contract].copy()
    refund_amount = float(related_rows.loc[related_rows["操作"] == "ETF 申购退款", "发生金额"].sum())
    cash_diff = float(related_rows.loc[related_rows["操作"] == "ETF 现金差额", "发生金额"].sum())
    domestic_pnl = refund_amount + cash_diff - redemption_cost

    return {
        "redemption_date": redemption_date,
        "redemption_contract": redemption_contract,
        "redemption_qty": redemption_qty,
        "redemption_cost": redemption_cost,
        "redemption_matches": redemption_matches,
        "refund_amount": refund_amount,
        "cash_diff": cash_diff,
        "domestic_pnl": domestic_pnl,
        "sell_records": sell_records,
    }


def load_ib_xop_rows() -> list[dict]:
    rows = []
    with IB_PATH.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.reader(f)
        for raw in reader:
            if len(raw) < 16:
                continue
            if raw[0] != "交易" or raw[1] != "Data" or raw[5] != "XOP":
                continue
            rows.append(
                {
                    "datetime": raw[6],
                    "qty": float(raw[7].replace(",", "")),
                    "price": float(raw[8].replace(",", "")),
                    "gross": float(raw[10].replace(",", "")),
                    "commission": abs(float(raw[11].replace(",", ""))),
                    "code": raw[15],
                }
            )
    return rows


def take_partial_trade(row: dict, qty: float) -> dict:
    scale = qty / abs(row["qty"])
    direction = -1 if row["qty"] < 0 else 1
    return {
        "datetime": row["datetime"],
        "qty": direction * qty,
        "price": row["price"],
        "gross": abs(row["gross"]) * scale,
        "commission": row["commission"] * scale,
        "code": row["code"],
    }


def analyze_ib() -> dict:
    rows = load_ib_xop_rows()

    open_rows = []
    remaining = TARGET_HEDGE_SHARES
    close_start = None

    for idx, row in enumerate(rows):
        if row["qty"] >= 0:
            close_start = idx
            break
        take_qty = min(abs(row["qty"]), remaining)
        open_rows.append(take_partial_trade(row, take_qty))
        remaining -= take_qty
        if remaining <= 1e-9:
            close_start = idx + 1
            break

    if remaining > 1e-9 or close_start is None:
        raise ValueError("Failed to find the opening hedge interval for 990 shares")

    close_rows = []
    remaining = TARGET_HEDGE_SHARES
    for row in rows[close_start:]:
        if row["qty"] <= 0:
            continue
        take_qty = min(row["qty"], remaining)
        close_rows.append(take_partial_trade(row, take_qty))
        remaining -= take_qty
        if remaining <= 1e-9:
            break

    if remaining > 1e-9:
        raise ValueError("Failed to find the closing hedge interval for 990 shares")

    sell_gross = sum(row["gross"] for row in open_rows)
    sell_commission = sum(row["commission"] for row in open_rows)
    buy_gross = sum(row["gross"] for row in close_rows)
    buy_commission = sum(row["commission"] for row in close_rows)
    usd_pnl = sell_gross - sell_commission - buy_gross - buy_commission

    return {
        "target_shares": TARGET_HEDGE_SHARES,
        "open_rows": open_rows,
        "close_rows": close_rows,
        "sell_gross": sell_gross,
        "sell_commission": sell_commission,
        "buy_gross": buy_gross,
        "buy_commission": buy_commission,
        "usd_pnl": usd_pnl,
        "cnh_pnl": usd_pnl * FX_RATE,
        "fx_rate": FX_RATE,
    }


def main() -> None:
    qmt = analyze_qmt()
    ib = analyze_ib()

    print("QMT redemption contract:", qmt["redemption_contract"])
    print("QMT redemption date:", qmt["redemption_date"])
    print("QMT redemption qty:", qmt["redemption_qty"])
    print("QMT redemption cost:", round(qmt["redemption_cost"], 2))
    print("QMT refund amount:", round(qmt["refund_amount"], 2))
    print("QMT cash diff:", round(qmt["cash_diff"], 2))
    print("QMT domestic pnl:", round(qmt["domestic_pnl"], 2))
    print("IB hedge shares:", ib["target_shares"])
    print("IB usd pnl:", round(ib["usd_pnl"], 6))
    print("IB cnh pnl:", round(ib["cnh_pnl"], 2))
    print("Total pnl cny:", round(qmt["domestic_pnl"] + ib["cnh_pnl"], 2))


if __name__ == "__main__":
    main()
