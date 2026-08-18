import unittest
from datetime import date, datetime
from decimal import Decimal

import redemption_engine as engine


def ib_slice(trade_id: str, qty: int, gross: str, commission: str, side: str) -> engine.IbSlice:
    return engine.IbSlice(
        trade_id=trade_id,
        dt=datetime(2026, 7, 1),
        side=side,
        qty=qty,
        price=Decimal(gross) / Decimal(qty),
        gross=Decimal(gross),
        commission=Decimal(commission),
    )


def settled_cash_flows() -> tuple[engine.CashFlow, ...]:
    return (
        engine.CashFlow("QMT1", date(2026, 7, 9), 1, "ETF 现金差额", Decimal("1"), 1),
        engine.CashFlow("QMT1", date(2026, 7, 14), 2, "ETF 申购退款", Decimal("1"), 2),
    )


class IbTailSettlementTests(unittest.TestCase):
    def test_pnl_uses_only_equal_closed_quantity(self) -> None:
        opening = (
            ib_slice("open-1", 9, "900", "9", "SELL"),
            ib_slice("open-tail", 1, "150", "1", "SELL"),
        )
        closing = (ib_slice("close-1", 9, "810", "9", "BUY"),)

        matched_qty, pnl = engine._closed_short_pnl(opening, closing)

        self.assertEqual(matched_qty, 9)
        self.assertEqual(pnl, Decimal("72"))
        self.assertNotEqual(pnl, Decimal("221"))

    def test_one_share_small_tail_is_settled_but_kept_auditable(self) -> None:
        basket = engine.BasketResult(
            id="tail-accepted",
            sequence=1,
            source="QMT1",
            redeem_day=date(2026, 7, 6),
            contract_no=1,
            redeem_qty=1_000_000,
            hedge_target=990,
            ib_open=(ib_slice("open", 990, "153450", "4.95", "SELL"),),
            ib_close=(ib_slice("close", 989, "152306", "4.945", "BUY"),),
            ib_close_shortfall=1,
            cash_flows=settled_cash_flows(),
        )
        _matched_qty, basket.ib_trade_pnl_usd = engine._closed_short_pnl(
            basket.ib_open,
            basket.ib_close,
        )

        engine.finalize_baskets([basket], Decimal("7"), date(2026, 7, 15), frozenset())

        self.assertEqual(basket.status, "已结算")
        self.assertEqual(basket.ib_matched_qty, 989)
        self.assertTrue(basket.ib_close_tail_accepted)
        self.assertIn("IB平仓尾差 1 股已放行", " | ".join(basket.warnings))
        self.assertIn("盈亏仅按实际闭合 989 股计算", " | ".join(basket.warnings))

    def test_one_share_large_relative_shortfall_is_not_tolerated(self) -> None:
        basket = engine.BasketResult(
            id="tail-rejected",
            sequence=1,
            source="QMT1",
            redeem_day=date(2026, 7, 6),
            contract_no=1,
            redeem_qty=10_000,
            hedge_target=10,
            ib_open=(ib_slice("open", 10, "1500", "1", "SELL"),),
            ib_close=(ib_slice("close", 9, "1350", "1", "BUY"),),
            ib_close_shortfall=1,
            cash_flows=settled_cash_flows(),
        )

        engine.finalize_baskets([basket], Decimal("7"), date(2026, 7, 15), frozenset())

        self.assertEqual(basket.status, "IB未完全匹配")
        self.assertFalse(basket.ib_close_tail_accepted)


if __name__ == "__main__":
    unittest.main()
