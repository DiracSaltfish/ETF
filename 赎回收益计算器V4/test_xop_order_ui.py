from __future__ import annotations

import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtCore import Qt
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QMessageBox

import realtime_premium
import xop_close_orders
from redemption_ui import XopCloseOrdersTab


class XopOrderUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    @staticmethod
    def connected_client(position: int = 20_000) -> realtime_premium.TwsXopMarketData:
        client = realtime_premium.TwsXopMarketData("127.0.0.1", 7496, 8888)
        with client._lock:
            client._managed_accounts = ("U1234567",)
            client._xop_positions = {"U1234567": position}
            client._xop_external_pending_sells = {"U1234567": 0}
        return client

    def test_creation_page_previews_four_balanced_sell_orders(self) -> None:
        client = self.connected_client()
        tab = XopCloseOrdersTab(client, xop_close_orders.XopCloseIntent.CREATION_SELL)
        tab.trade_date_edit.setText("20300107")
        tab.basket_count_spin.setValue(12)
        tab.total_qty_spin.setValue(990)
        tab.generate_preview()

        self.assertEqual(tab.order_table.rowCount(), 4)
        self.assertEqual(
            [tab.order_table.item(row, 2).text() for row in range(4)],
            ["2970"] * 4,
        )
        self.assertEqual(
            [tab.order_table.item(row, 6).text() for row in range(4)],
            ["SELL"] * 4,
        )
        self.assertTrue(
            all(tab.order_table.item(row, 0).checkState() == Qt.Unchecked for row in range(4))
        )
        self.assertIn("申购国内-卖出XOP", tab.event_log.toPlainText())
        tab.close()

    def test_creation_confirmation_names_business_side_and_defaults_to_no(self) -> None:
        client = self.connected_client()
        tab = XopCloseOrdersTab(client, xop_close_orders.XopCloseIntent.CREATION_SELL)
        tab.trade_date_edit.setText("20300107")
        tab.generate_preview()
        tab.order_table.item(0, 0).setCheckState(Qt.Checked)
        tab.unlock_checkbox.setChecked(True)

        with (
            patch.object(client, "is_connected", return_value=True),
            patch("redemption_ui.QMessageBox.question", return_value=QMessageBox.No) as question,
        ):
            tab.send_selected_orders()

        text = question.call_args.args[2]
        self.assertIn("申购国内-卖出XOP", text)
        self.assertIn("方向：SELL", text)
        self.assertIn("建立或增加XOP空头", text)
        self.assertEqual(question.call_args.args[1], "逐笔融券卖出确认")
        self.assertEqual(question.call_args.args[-1], QMessageBox.No)
        tab.close()

    def test_redemption_buy_keeps_tws_default_account(self) -> None:
        client = self.connected_client()
        tab = XopCloseOrdersTab(client, xop_close_orders.XopCloseIntent.REDEMPTION_BUY)
        tab.trade_date_edit.setText("20300107")
        tab.account_combo.clear()
        tab.generate_preview()

        self.assertTrue(all(not spec.account for spec in tab.specs_by_ref.values()))
        self.assertTrue(
            all(not getattr(xop_close_orders.build_ib_order(spec), "account", "")
                for spec in tab.specs_by_ref.values())
        )
        tab.order_table.item(0, 0).setCheckState(Qt.Checked)
        tab.unlock_checkbox.setChecked(True)
        with (
            patch.object(client, "is_connected", return_value=True),
            patch("redemption_ui.QMessageBox.question", return_value=QMessageBox.No) as question,
        ):
            tab.send_selected_orders()
        self.assertIn("IB账户：TWS默认账户", question.call_args.args[2])
        tab.close()

    def test_redemption_page_can_switch_from_legacy_to_four_balanced_buy_orders(self) -> None:
        client = self.connected_client()
        tab = XopCloseOrdersTab(client, xop_close_orders.XopCloseIntent.REDEMPTION_BUY)
        tab.trade_date_edit.setText("20300107")
        tab.total_qty_spin.setValue(990)

        self.assertEqual(tab.time_plan_combo.currentData(), "legacy")
        tab.generate_preview()
        self.assertEqual(tab.order_table.rowCount(), 5)

        tab.time_plan_combo.setCurrentIndex(tab.time_plan_combo.findData("balanced"))
        tab.generate_preview()

        self.assertEqual(tab.order_table.rowCount(), 4)
        self.assertEqual(
            [tab.order_table.item(row, 2).text() for row in range(4)],
            ["248", "248", "247", "247"],
        )
        self.assertEqual(
            [tab.order_table.item(row, 6).text() for row in range(4)],
            ["BUY"] * 4,
        )
        self.assertEqual(
            [tab.order_table.item(row, 3).text().split()[1] for row in range(4)],
            ["15:59:00", "15:59:15", "15:59:30", "15:59:45"],
        )
        self.assertEqual(tab.generate_button.text(), "生成4张订单预览（不发送）")
        tab.close()

    def test_redemption_quantity_is_keyboard_editable_and_refreshes_preview(self) -> None:
        client = self.connected_client()
        tab = XopCloseOrdersTab(client, xop_close_orders.XopCloseIntent.REDEMPTION_BUY)
        tab.trade_date_edit.setText("20300107")
        tab.show()
        self.app.processEvents()

        self.assertFalse(tab.total_qty_spin.keyboardTracking())
        tab.total_qty_spin.setFocus()
        tab.total_qty_spin.lineEdit().selectAll()
        QTest.keyClicks(tab.total_qty_spin, "999")
        QTest.keyClick(tab.total_qty_spin, Qt.Key_Return)
        self.app.processEvents()

        self.assertEqual(tab.total_qty_spin.value(), 999)
        self.assertEqual(tab.total_qty_label.text(), "总目标 999 股")
        self.assertEqual(
            [tab.order_table.item(row, 2).text() for row in range(5)],
            ["200", "200", "200", "200", "199"],
        )
        tab.close()

    def test_pages_ignore_the_other_business_order_events(self) -> None:
        client = self.connected_client()
        redeem_tab = XopCloseOrdersTab(client, xop_close_orders.XopCloseIntent.REDEMPTION_BUY)
        create_tab = XopCloseOrdersTab(client, xop_close_orders.XopCloseIntent.CREATION_SELL)
        before = redeem_tab.event_log.toPlainText()

        redeem_tab.handle_order_event(
            {
                "event": "orderStatus",
                "order_ref": "XOP_CREATE_CLOSE_20300107_155900_248_U1234567",
                "status": "PreSubmitted",
            }
        )

        self.assertEqual(redeem_tab.event_log.toPlainText(), before)
        redeem_tab.close()
        create_tab.close()


class TwsCreationShortSellTest(unittest.TestCase):
    def test_creation_sell_can_open_short_from_zero_position(self) -> None:
        client = XopOrderUiTest.connected_client(position=0)
        client.is_connected = lambda: True
        specs = xop_close_orders.generate_order_specs(
            xop_close_orders.parse_trade_date("20300107"),
            999,
            intent=xop_close_orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )

        self.assertTrue(all(client.submit_confirmed_order(spec) for spec in specs))
        state = client.account_state()["states"]["U1234567"]
        self.assertEqual(state["session_sell"], 999)
        self.assertEqual(state["projected_position"], -999)
        client.disconnect_tws()

    def test_same_order_cannot_be_queued_twice(self) -> None:
        client = XopOrderUiTest.connected_client(position=990)
        client.is_connected = lambda: True
        spec = xop_close_orders.generate_order_specs(
            xop_close_orders.parse_trade_date("20300107"),
            990,
            intent=xop_close_orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )[0]

        self.assertTrue(client.submit_confirmed_order(spec))
        self.assertFalse(client.submit_confirmed_order(spec))
        client.disconnect_tws()

    def test_projected_position_includes_other_and_session_sells(self) -> None:
        client = XopOrderUiTest.connected_client(position=990)
        client.is_connected = lambda: True
        spec = xop_close_orders.generate_order_specs(
            xop_close_orders.parse_trade_date("20300107"),
            990,
            intent=xop_close_orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )[0]
        self.assertTrue(client.submit_confirmed_order(spec))

        with client._lock:
            client._xop_external_pending_sells["U1234567"] = 800
        state = client.account_state()["states"]["U1234567"]
        self.assertEqual(state["projected_position"], -58)
        client.disconnect_tws()

    def test_partial_fill_reduces_projected_sell_commitment(self) -> None:
        client = XopOrderUiTest.connected_client(position=-1916)
        client.is_connected = lambda: True
        spec = xop_close_orders.generate_order_specs(
            xop_close_orders.parse_trade_date("20300107"),
            1000,
            intent=xop_close_orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )[0]
        self.assertTrue(client.submit_confirmed_order(spec))

        client._sync_creation_sell_commitment(
            SimpleNamespace(
                order=SimpleNamespace(orderRef=spec.order_ref),
                orderStatus=SimpleNamespace(status="Submitted", remaining=100),
            )
        )
        state = client.account_state()["states"]["U1234567"]
        self.assertEqual(state["session_sell"], 100)
        self.assertEqual(state["projected_position"], -2016)

        client._sync_creation_sell_commitment(
            SimpleNamespace(
                order=SimpleNamespace(orderRef=spec.order_ref),
                orderStatus=SimpleNamespace(status="Filled", remaining=0),
            )
        )
        state = client.account_state()["states"]["U1234567"]
        self.assertEqual(state["session_sell"], 0)
        client.disconnect_tws()

    def test_open_order_zero_remaining_does_not_drop_sell_commitment(self) -> None:
        client = XopOrderUiTest.connected_client(position=-1916)
        client.is_connected = lambda: True
        spec = xop_close_orders.generate_order_specs(
            xop_close_orders.parse_trade_date("20300107"),
            1000,
            intent=xop_close_orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )[0]
        self.assertTrue(client.submit_confirmed_order(spec))

        client._sync_creation_sell_commitment(
            SimpleNamespace(
                order=SimpleNamespace(orderRef=spec.order_ref),
                orderStatus=SimpleNamespace(status="PreSubmitted", remaining=0),
            )
        )

        state = client.account_state()["states"]["U1234567"]
        self.assertEqual(state["session_sell"], spec.quantity)
        self.assertEqual(state["projected_position"], -1916 - spec.quantity)
        client.disconnect_tws()

    def test_send_time_snapshot_includes_other_client_sell(self) -> None:
        client = XopOrderUiTest.connected_client(position=-1916)
        client.is_connected = lambda: True
        events: list[dict[str, object]] = []
        client.orderEvent.connect(events.append)
        spec = xop_close_orders.generate_order_specs(
            xop_close_orders.parse_trade_date("20300107"),
            990,
            intent=xop_close_orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )[0]
        self.assertTrue(client.submit_confirmed_order(spec))

        contract = SimpleNamespace(conId=1, symbol="XOP", secType="STK")
        external_sell = SimpleNamespace(
            contract=contract,
            order=SimpleNamespace(action="SELL", orderRef="MANUAL_SELL", account="U1234567"),
            orderStatus=SimpleNamespace(remaining=800),
        )

        class FakeIb:
            RequestTimeout = 30

            def __init__(self):
                self.placed_orders = []

            def managedAccounts(self):
                return ["U1234567"]

            def positions(self):
                return [SimpleNamespace(account="U1234567", contract=contract, position=-1916)]

            def portfolio(self):
                return [SimpleNamespace(account="U1234567", contract=contract, position=-1916)]

            def reqAllOpenOrders(self):
                return [external_sell]

            def placeOrder(self, _contract, order):
                self.placed_orders.append(order)
                order.orderId = 77
                return SimpleNamespace(
                    order=order,
                    orderStatus=SimpleNamespace(status="PreSubmitted"),
                )

        fake_ib = FakeIb()
        client._process_order_queue(fake_ib, contract)

        self.assertEqual(events[-1]["event"], "submitted")
        self.assertEqual(fake_ib.placed_orders[0].action, "SELL")
        self.assertEqual(fake_ib.placed_orders[0].account, "U1234567")
        state = client.account_state()["states"]["U1234567"]
        self.assertEqual(state["pending_sell"], 800)
        self.assertEqual(state["projected_position"], -2964)
        client.disconnect_tws()

    def test_portfolio_position_overrides_stale_positions_cache(self) -> None:
        client = XopOrderUiTest.connected_client(position=0)
        contract = SimpleNamespace(conId=1, symbol="XOP", secType="STK")

        class FakeIb:
            def managedAccounts(self):
                return ["U1234567"]

            def positions(self):
                return [SimpleNamespace(account="U1234567", contract=contract, position=0)]

            def portfolio(self):
                return [SimpleNamespace(account="U1234567", contract=contract, position=-1916)]

            def openTrades(self):
                return []

        client._refresh_account_state(FakeIb(), contract)

        state = client.account_state()["states"]["U1234567"]
        self.assertEqual(state["position"], -1916)
        self.assertEqual(state["projected_position"], -1916)

    def test_send_time_snapshot_failure_blocks_creation_sell(self) -> None:
        client = XopOrderUiTest.connected_client(position=990)
        client.is_connected = lambda: True
        events: list[dict[str, object]] = []
        client.orderEvent.connect(events.append)
        spec = xop_close_orders.generate_order_specs(
            xop_close_orders.parse_trade_date("20300107"),
            990,
            intent=xop_close_orders.XopCloseIntent.CREATION_SELL,
            account="U1234567",
        )[0]
        self.assertTrue(client.submit_confirmed_order(spec))

        class FailingIb:
            RequestTimeout = 30

            def reqAllOpenOrders(self):
                raise TimeoutError("全量订单快照超时")

            def placeOrder(self, _contract, _order):
                raise AssertionError("全量快照失败时不应发单")

        client._process_order_queue(
            FailingIb(),
            SimpleNamespace(conId=1, symbol="XOP", secType="STK"),
        )

        self.assertIn("全量订单快照超时", str(events[-1]["message"]))
        client.disconnect_tws()


if __name__ == "__main__":
    unittest.main()
