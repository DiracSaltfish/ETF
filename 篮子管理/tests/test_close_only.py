from __future__ import annotations

import unittest
from datetime import datetime, timezone
from pathlib import Path

from basket_models import BasketDocument, BasketItem, PortfolioPosition
from close_only import (
    ActiveOrderSnapshot,
    build_close_only_plan,
    campaign_basis_from_dict,
    campaign_basis_to_dict,
    compute_base_sell_due,
    plans_match_for_execution,
)


ACCOUNT = "U_TEST"


def basket(*component_symbols: str) -> BasketDocument:
    rows = [BasketItem("XOP", "BUY", 200)]
    rows.extend(BasketItem(symbol, "SELL", 1) for symbol in component_symbols)
    return BasketDocument(Path("/tmp/test_basket.xlsx"), "test", tuple(rows))


def position(
    symbol: str,
    quantity: float,
    price: float,
    con_id: int,
    *,
    account: str = ACCOUNT,
) -> PortfolioPosition:
    return PortfolioPosition(
        account=account,
        symbol=symbol,
        local_symbol=symbol,
        sec_type="STK",
        exchange="NYSE",
        currency="USD",
        quantity=quantity,
        avg_cost=price,
        market_price=price,
        market_value=quantity * price,
        unrealized_pnl=0,
        realized_pnl=0,
        con_id=con_id,
    )


def neutral_positions() -> tuple[PortfolioPosition, ...]:
    return (
        position("AAA", -100, 10, 101),
        position("BBB", -50, 20, 102),
        position("XOP", 200, 10, 999),
    )


class CloseOnlyPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.basket = basket("AAA", "BBB")
        self.now = datetime(2026, 7, 21, 9, 30, tzinfo=timezone.utc)

    def test_full_close_only_moves_every_leg_to_zero(self) -> None:
        plan = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=100,
            now=self.now,
        )

        self.assertTrue(plan.can_execute)
        self.assertFalse(plan.blockers)
        self.assertEqual(2, len(plan.component_lines))
        self.assertTrue(all(line.action == "BUY" for line in plan.component_lines))
        self.assertTrue(all(line.projected_position == 0 for line in plan.component_lines))
        self.assertEqual(200, plan.total_base_sell_qty)
        self.assertEqual("SELL", plan.base_lines[0].action)
        self.assertEqual(0, plan.base_lines[0].projected_position)
        self.assertEqual(("AAA", "BBB", "XOP"), plan.basis.scope_symbols)

    def test_partial_batch_uses_fixed_campaign_reference_notional(self) -> None:
        plan = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
            now=self.now,
        )
        quantities = {line.symbol: line.quantity for line in plan.lines}

        self.assertEqual(25, quantities["AAA"])
        self.assertEqual(13, quantities["BBB"])
        self.assertEqual(51, quantities["XOP"])

    def test_pending_cancel_is_still_an_active_order_blocker(self) -> None:
        order = ActiveOrderSnapshot(
            account=ACCOUNT,
            con_id=101,
            symbol="AAA",
            action="BUY",
            total_quantity=10,
            filled=10,
            remaining=0,
            status="PendingCancel",
            order_id=17,
        )
        plan = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            active_orders=(order,),
            tranche_percent=25,
        )

        self.assertFalse(plan.can_execute)
        self.assertTrue(any("活动订单" in item for item in plan.blockers))

    def test_external_position_expansion_is_blocked(self) -> None:
        initial = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
        )
        expanded = (
            position("AAA", -101, 10, 101),
            position("BBB", -50, 20, 102),
            position("XOP", 200, 10, 999),
        )
        plan = build_close_only_plan(
            self.basket,
            expanded,
            account=ACCOUNT,
            tranche_percent=25,
            campaign_basis=initial.basis,
        )

        self.assertFalse(plan.can_execute)
        self.assertTrue(any("超过会话初始仓位" in item for item in plan.blockers))

    def test_duplicate_contract_rows_are_blocked(self) -> None:
        positions = neutral_positions() + (position("AAA", -1, 10, 555),)
        plan = build_close_only_plan(
            self.basket,
            positions,
            account=ACCOUNT,
            tranche_percent=25,
        )
        self.assertFalse(plan.can_execute)
        self.assertTrue(any("多条合约" in item for item in plan.blockers))

    def test_fractional_or_wrong_direction_positions_are_blocked(self) -> None:
        positions = (
            position("AAA", -99.5, 10, 101),
            position("BBB", 5, 20, 102),
            position("XOP", -2, 10, 999),
        )
        plan = build_close_only_plan(
            self.basket,
            positions,
            account=ACCOUNT,
            tranche_percent=25,
        )
        joined = " | ".join(plan.blockers)
        self.assertIn("碎股", joined)
        self.assertIn("已是多头", joined)
        self.assertIn("已是空头", joined)

    def test_same_path_basket_scope_change_is_blocked(self) -> None:
        initial = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
        )
        changed_basket = basket("AAA")
        plan = build_close_only_plan(
            changed_basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
            campaign_basis=initial.basis,
        )
        self.assertTrue(any("成分范围" in item for item in plan.blockers))

    def test_approval_is_invalidated_by_position_change(self) -> None:
        approved = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
        )
        fresh_same = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
            campaign_basis=approved.basis,
        )
        changed = list(neutral_positions())
        changed[0] = position("AAA", -99, 10, 101)
        fresh_changed = build_close_only_plan(
            self.basket,
            tuple(changed),
            account=ACCOUNT,
            tranche_percent=25,
            campaign_basis=approved.basis,
        )

        self.assertTrue(plans_match_for_execution(approved, fresh_same))
        self.assertFalse(plans_match_for_execution(approved, fresh_changed))

    def test_base_sell_due_uses_observed_component_positions(self) -> None:
        initial = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
        )
        after_fills = (
            position("AAA", -50, 10, 101),
            position("BBB", -25, 20, 102),
            position("XOP", 200, 10, 999),
        )
        self.assertEqual(100, compute_base_sell_due(initial.basis, after_fills))

    def test_base_sync_rejects_crossed_component(self) -> None:
        initial = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
        )
        crossed = (
            position("AAA", 1, 10, 101),
            position("BBB", -25, 20, 102),
            position("XOP", 200, 10, 999),
        )
        with self.assertRaisesRegex(ValueError, "穿越为多头"):
            compute_base_sell_due(initial.basis, crossed)

    def test_campaign_basis_round_trip_preserves_scope_and_prices(self) -> None:
        plan = build_close_only_plan(
            self.basket,
            neutral_positions(),
            account=ACCOUNT,
            tranche_percent=25,
        )
        restored = campaign_basis_from_dict(campaign_basis_to_dict(plan.basis))
        self.assertEqual(plan.basis, restored)


if __name__ == "__main__":
    unittest.main()
