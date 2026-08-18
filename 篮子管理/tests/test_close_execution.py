from __future__ import annotations

import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from basket_models import (
    BasketDocument,
    BasketItem,
    ConnectionSettings,
    ConnectionSnapshot,
    OrderMonitorRecord,
    SymbolMarketState,
)
from close_only import build_close_only_plan
from ib_service import (
    _trade_matches_monitor_record,
    _with_close_pricing_blockers,
    execute_close_only_plan,
)
from tests.test_close_only import ACCOUNT, neutral_positions, position


class FakeIB:
    def __init__(self, *, fail_on_place: int | None = None) -> None:
        self.fail_on_place = fail_on_place
        self.place_calls = []

    def placeOrder(self, contract, order):  # noqa: N802 - mirror IB API
        call_number = len(self.place_calls) + 1
        if self.fail_on_place == call_number:
            raise RuntimeError(f"simulated place failure {call_number}")
        order.orderId = 1000 + call_number
        order.permId = 2000 + call_number
        self.place_calls.append((contract, order))
        return SimpleNamespace(
            order=order,
            orderStatus=SimpleNamespace(status="Submitted", permId=order.permId),
        )

    def sleep(self, _seconds: float) -> None:
        return None


class CloseOnlyExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.basket = BasketDocument(
            Path("/tmp/execute_basket.xlsx"),
            "execute",
            (
                BasketItem("XOP", "BUY", 200),
                BasketItem("AAA", "SELL", 100),
                BasketItem("BBB", "SELL", 50),
            ),
        )
        self.positions = neutral_positions()
        self.plan = build_close_only_plan(
            self.basket,
            self.positions,
            account=ACCOUNT,
            tranche_percent=25,
        )
        self.settings = ConnectionSettings("127.0.0.1", 7496, 9001, ACCOUNT)
        self.snapshot = ConnectionSnapshot(
            host="127.0.0.1",
            port=7496,
            client_id=9001,
            managed_accounts=(ACCOUNT,),
            active_account=ACCOUNT,
            server_version=999,
            server_time="2026-07-21 09:30:00",
        )
        self.contracts = {
            line.symbol: SimpleNamespace(conId=line.con_id)
            for line in self.plan.lines
        }

    def run_execution(self, fake_ib: FakeIB, portfolio_side_effect):
        @contextmanager
        def fake_connection(_settings, **_kwargs):
            yield fake_ib

        state = (
            self.snapshot,
            self.positions,
            (),
            (),
            self.contracts,
            self.plan,
        )
        with (
            patch("ib_service.ib_connection", fake_connection),
            patch("ib_service._load_close_only_state", return_value=state),
            patch("ib_service._load_portfolio_positions", side_effect=portfolio_side_effect),
            patch("ib_service._load_active_order_snapshots", return_value=()),
            patch("ib_service.ensure_campaign"),
            patch("ib_service.record_event"),
            patch("ib_service.record_submitted_order"),
            patch("ib_service.mark_campaign_complete"),
        ):
            return execute_close_only_plan(
                self.settings,
                self.basket,
                self.plan,
                pricing_mode="MKT",
                tif="DAY",
                outside_rth=False,
            )

    def test_only_observed_component_fills_can_release_xop(self) -> None:
        projected = (
            position("AAA", -75, 10, 101),
            position("BBB", -37, 20, 102),
            position("XOP", 200, 10, 999),
        )
        after_xop = projected[:-1] + (position("XOP", 149, 10, 999),)
        fake_ib = FakeIB()
        result = self.run_execution(
            fake_ib,
            [self.positions, self.positions, projected, projected, after_xop],
        )

        self.assertEqual(["BUY", "BUY", "SELL"], [order.action for order in result.orders])
        self.assertEqual("XOP", result.orders[-1].symbol)
        self.assertEqual(51, result.orders[-1].quantity)
        self.assertTrue(all(order.order_ref.startswith("UW-") for order in result.orders))

    def test_live_position_cap_reduces_component_quantity(self) -> None:
        aaa_already_reduced = (
            position("AAA", -10, 10, 101),
            position("BBB", -50, 20, 102),
            position("XOP", 200, 10, 999),
        )
        fake_ib = FakeIB()
        result = self.run_execution(
            fake_ib,
            [aaa_already_reduced, self.positions, self.positions, self.positions],
        )

        aaa_order = next(order for order in result.orders if order.symbol == "AAA")
        self.assertEqual(10, aaa_order.quantity)
        self.assertLessEqual(aaa_order.quantity, abs(aaa_already_reduced[0].quantity))

    def test_live_position_expansion_pauses_before_that_order(self) -> None:
        expanded = (
            position("AAA", -101, 10, 101),
            position("BBB", -50, 20, 102),
            position("XOP", 200, 10, 999),
        )
        fake_ib = FakeIB()
        result = self.run_execution(fake_ib, [expanded, self.positions])

        self.assertEqual("PAUSED", result.status)
        self.assertFalse(result.orders)
        self.assertIn("超过会话初始值", result.error)

    def test_partial_submission_is_returned_and_flow_pauses(self) -> None:
        fake_ib = FakeIB(fail_on_place=2)
        result = self.run_execution(
            fake_ib,
            [self.positions, self.positions, self.positions],
        )

        self.assertEqual("PAUSED", result.status)
        self.assertEqual(1, len(result.orders))
        self.assertEqual("AAA", result.orders[0].symbol)
        self.assertIn("simulated place failure", result.error)

    def test_post_submit_reconciliation_failure_still_returns_orders(self) -> None:
        fake_ib = FakeIB()
        result = self.run_execution(
            fake_ib,
            [self.positions, self.positions, self.positions],
        )

        self.assertEqual("PAUSED", result.status)
        self.assertEqual(2, len(result.orders))
        self.assertIn("提交后状态复核失败", result.error)

    def test_stale_plan_stops_before_any_order_or_campaign_write(self) -> None:
        changed_positions = (
            position("AAA", -99, 10, 101),
            position("BBB", -50, 20, 102),
            position("XOP", 200, 10, 999),
        )
        fresh = build_close_only_plan(
            self.basket,
            changed_positions,
            account=ACCOUNT,
            tranche_percent=25,
            campaign_basis=self.plan.basis,
        )
        fake_ib = FakeIB()

        @contextmanager
        def fake_connection(_settings, **_kwargs):
            yield fake_ib

        state = (
            self.snapshot,
            changed_positions,
            (),
            (),
            self.contracts,
            fresh,
        )
        with (
            patch("ib_service.ib_connection", fake_connection),
            patch("ib_service._load_close_only_state", return_value=state),
            patch("ib_service.ensure_campaign") as ensure,
        ):
            with self.assertRaisesRegex(ValueError, "计划已失效"):
                execute_close_only_plan(
                    self.settings,
                    self.basket,
                    self.plan,
                    pricing_mode="MKT",
                    tif="DAY",
                    outside_rth=False,
                )

        self.assertFalse(fake_ib.place_calls)
        ensure.assert_not_called()

    def test_outside_rth_is_rejected_before_connect(self) -> None:
        with patch("ib_service.ib_connection") as connect:
            with self.assertRaisesRegex(ValueError, "禁止 Outside RTH"):
                execute_close_only_plan(
                    self.settings,
                    self.basket,
                    self.plan,
                    pricing_mode="MKT",
                    tif="DAY",
                    outside_rth=True,
                )
        connect.assert_not_called()

    def test_gtc_is_rejected_before_connect(self) -> None:
        with patch("ib_service.ib_connection") as connect:
            with self.assertRaisesRegex(ValueError, "仅允许 DAY"):
                execute_close_only_plan(
                    self.settings,
                    self.basket,
                    self.plan,
                    pricing_mode="MKT",
                    tif="GTC",
                    outside_rth=False,
                )
        connect.assert_not_called()

    def test_monitor_identity_requires_account_symbol_action_conid_and_ref(self) -> None:
        record = OrderMonitorRecord(
            batch_id="B1",
            group_label="Close Only",
            submitted_at="now",
            symbol="AAA",
            action="BUY",
            quantity=25,
            order_type="MKT",
            limit_price=None,
            order_id=1,
            perm_id=2,
            status="Submitted",
            con_id=101,
            order_ref="UW-REF",
            account=ACCOUNT,
        )
        trade = SimpleNamespace(
            contract=SimpleNamespace(symbol="AAA", conId=101),
            order=SimpleNamespace(account=ACCOUNT, action="BUY", orderRef="UW-REF"),
        )
        self.assertTrue(_trade_matches_monitor_record(trade, record))
        trade.order.account = "OTHER"
        self.assertFalse(_trade_matches_monitor_record(trade, record))

    def test_opponent_price_preview_requires_quote_for_every_line(self) -> None:
        missing = _with_close_pricing_blockers(self.plan, (), "OPPONENT")
        self.assertFalse(missing.can_execute)
        states = tuple(
            SymbolMarketState(
                symbol=line.symbol,
                market_price=10,
                bid=9.99,
                ask=10.01,
            )
            for line in self.plan.lines
        )
        ready = _with_close_pricing_blockers(self.plan, states, "OPPONENT")
        self.assertFalse(ready.blockers)


if __name__ == "__main__":
    unittest.main()
