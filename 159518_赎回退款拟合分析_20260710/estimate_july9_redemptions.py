#!/usr/bin/env python3
"""Standalone domestic cash/P&L estimate for the two 2026-07-09 redemptions.

The redemption calculator and its data stay read-only.  Historical QMT delivery
records are used for pre-existing FIFO lots; the 20260709 live QMT logs supply
the two newly observed redemption instructions and same-day purchases.
"""

from __future__ import annotations

import csv
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_ROOT = ROOT.parent / "赎回收益计算器"
LIVE_ROOT = Path("/Users/ellis/Desktop/交易表格ETF/20260709")
OUT_DIR = ROOT / "output"
DAY = date(2026, 7, 9)
SHARES_PER_CU = Decimal("996")
MODEL_FX_TIME = "16:00"
Q2 = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(Q2, rounding=ROUND_HALF_UP)


@dataclass
class Lot:
    qty: int
    cost: Decimal
    origin: str


@dataclass(frozen=True)
class LiveRecord:
    source: str
    dt: datetime
    action: str
    qty: int
    amount: Decimal
    order_no: str


def load_live_records(source: str) -> list[LiveRecord]:
    path = LIVE_ROOT / f"{source}.csv"
    result: list[LiveRecord] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("证券代码(原始)") != "159518" or row.get("委托状态") != "已成交":
                continue
            qty = int(row["成交数量"])
            if qty <= 0:
                continue
            is_redemption = row.get("买卖方向") == "卖出" and row.get("成交金额") in {"0", "0.0", "0.00"}
            if is_redemption:
                action, amount = "ETF 基金赎回", Decimal("0")
            elif row.get("买卖方向") == "买入":
                action, amount = "证券买入", -Decimal(row["成交金额"])
            else:
                action, amount = "证券卖出", Decimal(row["成交金额"])
            dt = datetime.strptime(f"{row['委托日期']}{row['委托时间'].zfill(6)}", "%Y%m%d%H%M%S")
            result.append(LiveRecord(source, dt, action, qty, amount, row["委托号"]))
    return sorted(result, key=lambda item: (item.dt, item.order_no))


def consume(lots: list[Lot], qty: int) -> tuple[Decimal, int, list[str]]:
    cost = Decimal("0")
    remaining = qty
    origins: list[str] = []
    while remaining and lots:
        lot = lots[0]
        used = min(remaining, lot.qty)
        cost += lot.cost * Decimal(used) / Decimal(lot.qty)
        origins.append(f"{used}@{lot.origin}")
        lot.qty -= used
        remaining -= used
        if lot.qty == 0:
            lots.pop(0)
    return cost, remaining, origins


def apply(action: str, qty: int, amount: Decimal, lots: list[Lot], origin: str) -> tuple[Decimal, int, list[str]]:
    if action == "证券买入":
        lots.append(Lot(qty, abs(amount), origin))
        return Decimal("0"), 0, []
    if action in {"证券卖出", "ETF 基金赎回"}:
        return consume(lots, qty)
    return Decimal("0"), 0, []


def t0_fx() -> Decimal:
    with (SOURCE_ROOT / "fx_data" / "fx_rates.csv").open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if (
                row.get("trade_date") == DAY.isoformat()
                and row.get("source") == "CFETS_REFERENCE_RATE"
                and row.get("pair") == "USD/CNY"
                and row.get("quote_time") == MODEL_FX_TIME
            ):
                return Decimal(row["rate"])
    raise KeyError(f"本地 CFETS 文件缺少 2026-07-09 {MODEL_FX_TIME}")


def model_price() -> Decimal:
    path = ROOT / "data" / "xop_tail_1m_20260709.csv"
    with path.open(encoding="utf-8-sig", newline="") as handle:
        row = next((row for row in csv.DictReader(handle) if row["minute"] == "15:59"), None)
    if row is None:
        raise ValueError("缺少 15:59 XOP 分钟 bar")
    return Decimal(row["close"])


def pcf_estimate_cash() -> Decimal:
    sys.path.insert(0, str(SOURCE_ROOT))
    import szse_pcf

    detail = szse_pcf.SzsePcfStore(SOURCE_ROOT / "szse_pcf_cache").ensure_fund_detail(DAY, "159518")
    return Decimal(detail.metadata["EstimateCashComponent"])


def conditional_ib_short_mark(price: Decimal) -> tuple[int, Decimal, Decimal, Decimal, Decimal]:
    """Return qty, average sell price, USD/CNY, and a pro-rata 1,994-share mark.

    The live file includes more XOP short sales than the two modeled baskets.
    Therefore this is deliberately a conditional, pro-rata estimate rather than
    an asserted trade-to-basket allocation.
    """
    path = LIVE_ROOT / "IB.csv"
    rows: list[dict[str, str]] = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row.get("代码") == "XOP" and row.get("买卖") == "SELL":
                rows.append(row)
    qty = sum((int(row["成交数量"]) for row in rows), 0)
    gross = sum((Decimal(row["成交金额"]) for row in rows), Decimal("0"))
    average = gross / Decimal(qty)
    ib_fx = Decimal(rows[0]["当日USDCNH汇率"])
    target = int(SHARES_PER_CU * 2)
    pnl_usd = (average - price) * Decimal(target)
    return qty, average, ib_fx, pnl_usd, money(pnl_usd * ib_fx)


def main() -> int:
    sys.path.insert(0, str(SOURCE_ROOT))
    import redemption_engine as engine

    config = json.loads((SOURCE_ROOT / "config.json").read_text(encoding="utf-8"))
    historical = {
        source: engine.load_qmt_file(config[f"{source.lower()}_path"], source)
        for source in ("QMT1", "QMT2")
    }
    live = {source: load_live_records(source) for source in ("QMT1", "QMT2")}
    fx = t0_fx()
    price = model_price()
    refund = money(SHARES_PER_CU * price * fx)
    estimated_cash = money(pcf_estimate_cash())
    estimated_total = refund + estimated_cash

    result_rows: list[dict[str, object]] = []

    def add_redemption_row(source: str, redeem_dt: datetime, qty: int, consumed_cost: Decimal, shortfall: int, origins: list[str]) -> None:
        result_rows.append(
            {
                "source": source,
                "redeem_time": redeem_dt.isoformat(sep=" "),
                "redeem_qty": qty,
                "fifo_domestic_cost_cny": money(consumed_cost),
                "inventory_shortfall_qty": shortfall,
                "xop_shares_model": SHARES_PER_CU,
                "xop_model_price_1559_usd": price,
                "cfets_t0_1600": fx,
                "estimated_refund_cny": refund,
                "pcf_estimated_cash_difference_cny": estimated_cash,
                "estimated_total_domestic_return_cny": estimated_total,
                "estimated_domestic_pnl_cny": money(estimated_total - consumed_cost) if not shortfall else "",
                "fifo_lot_trace": " | ".join(origins),
                "scope_note": "国内预测回款减更新交割单FIFO成本；未含IB对冲、借券、佣金和未来实际结算差异",
            }
        )

    for source in ("QMT1", "QMT2"):
        lots: list[Lot] = []
        delivery_records = sorted(historical[source], key=lambda item: (item.trade_day, item.contract_no, item.row_number))
        delivery_has_redemption = any(
            item.trade_day == DAY and item.action == "ETF 基金赎回" for item in delivery_records
        )
        live_redemption_times = iter(
            item.dt for item in live[source] if item.action == "ETF 基金赎回"
        )
        if delivery_has_redemption:
            # Updated delivery files are authoritative for quantities and fees;
            # the live file contributes only the intraday redemption timestamp.
            for record in delivery_records:
                consumed_cost, shortfall, origins = apply(
                    record.action, record.qty, record.amount, lots, f"交割单:{record.trade_day}:{record.contract_no}"
                )
                if record.trade_day == DAY and record.action == "ETF 基金赎回":
                    redeem_dt = next(live_redemption_times, datetime.combine(DAY, datetime.min.time()))
                    add_redemption_row(source, redeem_dt, record.qty, consumed_cost, shortfall, origins)
        else:
            # Before the delivery file is updated, extend its prior-day FIFO with
            # the immutable live log exactly once.
            for record in delivery_records:
                if record.trade_day >= DAY:
                    continue
                apply(record.action, record.qty, record.amount, lots, f"交割单:{record.trade_day}:{record.contract_no}")
            for record in live[source]:
                consumed_cost, shortfall, origins = apply(
                    record.action, record.qty, record.amount, lots, f"实时:{record.dt:%H:%M:%S}:{record.order_no}"
                )
                if record.action == "ETF 基金赎回":
                    add_redemption_row(source, record.dt, record.qty, consumed_cost, shortfall, origins)

    if len(result_rows) != 2:
        raise ValueError(f"预期两笔 2026-07-09 赎回，实际识别 {len(result_rows)} 笔")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fields = list(result_rows[0])
    output_csv = OUT_DIR / "20260709_两篮子赎回预估.csv"
    with output_csv.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(result_rows)

    total_cost = sum((Decimal(str(row["fifo_domestic_cost_cny"])) for row in result_rows), Decimal("0"))
    total_return = sum((Decimal(str(row["estimated_total_domestic_return_cny"])) for row in result_rows), Decimal("0"))
    total_pnl = total_return - total_cost
    ib_qty, ib_average, ib_fx, ib_pnl_usd, ib_pnl_cny = conditional_ib_short_mark(price)
    ib_target = int(SHARES_PER_CU * 2)
    combined_conditional = total_pnl + ib_pnl_cny
    lines = [
        "# 2026-07-09 两篮子赎回预估",
        "",
        "模型：每篮 1,000,000 份；`996 × XOP 15:59（美东）1分钟收盘价 × 2026-07-09 CFETS 16:00`，再单列加入 PCF EstimateCashComponent。",
        "",
        f"- XOP 15:59 一分钟收盘价：{price:.6f} USD",
        f"- CFETS T 日 {MODEL_FX_TIME}：{fx:.4f}",
        f"- 每篮预计 ETF 申购退款：{refund:,.2f} CNY",
        f"- 每篮预计现金差额（PCF EstimateCashComponent）：{estimated_cash:,.2f} CNY",
        f"- 每篮预计国内总回款：{estimated_total:,.2f} CNY",
        "",
        "| 账户 | 赎回时间 | FIFO 国内成本 | 预计总回款 | 预计国内收益 |",
        "|---|---|---:|---:|---:|",
    ]
    for row in result_rows:
        pnl = row["estimated_domestic_pnl_cny"]
        lines.append(
            f"| {row['source']} | {row['redeem_time']} | {Decimal(str(row['fifo_domestic_cost_cny'])):,.2f} | "
            f"{Decimal(str(row['estimated_total_domestic_return_cny'])):,.2f} | {pnl if pnl == '' else Decimal(str(pnl))} |"
        )
    lines.extend(
        [
            "",
            f"两篮子合计预计国内总回款：**{total_return:,.2f} CNY**",
            f"两篮子合计预计国内收益：**{total_pnl:,.2f} CNY**",
            "",
            "## 条件性 IB 对冲估值（非已实现收益）",
            "",
            f"- 7 月 9 日实时 IB 文件有 XOP 卖出 **{ib_qty:,} 股**，比两篮子模型目标的 {ib_target:,} 股多 {ib_qty - ib_target:,} 股，无法从当前文件唯一分配到两篮子。",
            f"- 若按全部 XOP 卖出均价 {ib_average:.6f} USD，将其中 {ib_target:,} 股按比例归属两篮子，并以模型价 {price:.6f} USD 盯市：浮动收益约 **{ib_pnl_usd:,.2f} USD / {ib_pnl_cny:,.2f} CNY**（IB 文件当日 USDCNH {ib_fx:.6f}）。",
            f"- 在上述仅用于敏感性测算的分配假设下，两篮子“国内收益 + XOP 浮盈”约为 **{combined_conditional:,.2f} CNY**。",
            "",
            "限制：国内成本已使用当前更新交割单；退款与现金差额仍为预测值。IB 部分没有唯一篮子映射，且未含佣金、借券费、后续回补成交及实际汇兑损益，因此不能当作最终已实现总收益。",
        ]
    )
    (OUT_DIR / "20260709_两篮子赎回预估.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {output_csv}")
    print(f"Two-basket estimated domestic P&L: {total_pnl:,.2f} CNY")


if __name__ == "__main__":
    main()
