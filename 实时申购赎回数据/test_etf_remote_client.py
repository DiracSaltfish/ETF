#!/usr/bin/env python3

import os
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import QApplication, QLabel, QTabWidget

from etf_remote_client import (
    ChangeHistoryDialog,
    DoubleClickButton,
    QmtBackendClient,
    RemoteClientWindow,
    _qmt_snapshot_checksum,
    _qmt_sorted_orders,
    _qmt_sorted_positions,
    basket_count,
    classify_local_net_creation_quota,
    format_basket_count,
    format_share_value,
    format_time_only,
    resource_path,
    qmt_etf_code,
    split_address,
)


APP = QApplication.instance() or QApplication([])


class RemoteClientTest(unittest.TestCase):
    def test_change_history_dialog_renders_persisted_events(self) -> None:
        dialog = ChangeHistoryDialog(symbols=["159518"], selected_symbol="159518")
        payload = {
            "date": "2026-08-17",
            "items": [
                {
                    "event_time": "2026-08-17T14:24:00.619+08:00",
                    "symbol": "159518",
                    "name": "标普油气ETF嘉实",
                    "current": {
                        "etfbuyamount": 2_000_000,
                        "etfsellamount": 1_000_000,
                        "netamount": 1_000_000,
                    },
                    "changes": [
                        {"text": "申购份额 100 0000 → 200 0000"}
                    ],
                    "opportunity": {"label": "盘中赎回机会"},
                }
            ],
        }
        dialog.set_payload(payload)
        self.assertEqual(dialog.table.rowCount(), 1)
        self.assertEqual(dialog.table.item(0, 0).text(), "14:24:00.619")
        self.assertEqual(dialog.table.item(0, 3).text(), "200 0000")
        self.assertEqual(dialog.table.item(0, 5).text(), "+100 0000")
        self.assertIn("1 条变化", dialog.summary_label.text())
        self.assertEqual(
            format_time_only("2026-08-17T14:24:00.619+08:00"),
            "14:24:00.619",
        )
        dialog.close()

    def test_snapshot_and_change_trigger_local_alert(self) -> None:
        window = RemoteClientWindow()
        window.popup_check.setChecked(True)
        window.sound_check.setChecked(True)
        item = {
            "symbol": "159518",
            "name": "油气 ETF",
            "status": "monitoring",
            "values": {
                "etfbuynumber": 3,
                "etfbuyamount": 3_000_000,
                "etfsellnumber": 2,
                "etfsellamount": 2_000_000,
                "netamount": 1_000_000,
            },
            "updated_at": "2026-08-11T09:30:00+08:00",
            "last_change": [],
            "pcf": {
                "status": "ready",
                "trading_day": "2026-08-11",
                "creation_redemption_unit": 1_000_000,
                "creation_allowed": True,
                "creation_limit": 0,
                "net_creation_limit": 1_000_000,
            },
            "opportunity": {
                "kind": "creation",
                "label": "申购机会",
                "reason": "净申购 1.00 篮子",
            },
        }
        window._apply_snapshot(
            {"type": "snapshot", "items": [item], "monitoring": True}
        )
        self.assertEqual(window.table.item(0, 1).text(), "油气 ETF")
        self.assertEqual(window.table.item(0, 3).text(), "300 0000")
        self.assertEqual(window.table.item(0, 4).text(), "200 0000")
        self.assertEqual(window.table.item(0, 5).text(), "+100 0000")
        self.assertEqual(window.table.item(0, 6).text(), "3")
        self.assertEqual(window.table.item(0, 7).text(), "2")
        self.assertEqual(window.table.item(0, 8).text(), "+1")
        self.assertEqual(window.table.item(0, 9).text(), "0")
        self.assertEqual(window.table.item(0, 9).foreground().color().name(), "#9f1239")
        self.assertEqual(window.table.item(0, 9).background().color().name(), "#fde7e7")
        self.assertEqual(window.table.item(0, 10).text(), "申购机会")
        self.assertEqual(window.table.columnCount(), 13)
        self.assertEqual(
            [window.table.horizontalHeaderItem(column).text() for column in range(13)],
            [
                "标的",
                "名称",
                "主机状态",
                "申购份额",
                "赎回份额",
                "轧差份额",
                "申购篮子",
                "赎回篮子",
                "轧差篮子",
                "可申购篮子",
                "机会判断",
                "更新时间",
                "最近变化",
            ],
        )
        self.assertEqual(window.table.item(0, 11).text(), "09:30:00")
        self.assertTrue(window.baseline_established)
        self.assertTrue(window.reset_baseline_button.isEnabled())

        changed_item = dict(item)
        changed_item["values"] = dict(item["values"], netamount=2_000_000)
        changed_item["last_change"] = [
            {"text": "轧差份额 +1,000,000 → +2,000,000"}
        ]
        changed_item["opportunity"] = {
            "kind": "creation",
            "label": "盘中申购机会",
            "reason": "盘中净申购增加",
        }
        event = {
            "type": "change",
            "items": [
                {
                    "symbol": "159518",
                    "current": changed_item,
                    "changes": [
                        {
                            "field": "netamount",
                            "text": "轧差份额 +1,000,000 → +2,000,000",
                        }
                    ],
                }
            ],
        }
        with patch.object(window, "play_alert_sound", return_value=True) as play_sound, patch(
            "etf_remote_client.QApplication.beep"
        ) as beep:
            window._apply_change(event)
            play_sound.assert_called_once()
            beep.assert_not_called()
        self.assertEqual(len(window.alert_popups), 1)
        alert_text = window.alert_popups[0].findChildren(type(window.connection_label))[1].text()
        self.assertIn("159518", alert_text)
        self.assertIn("油气 ETF", alert_text)
        self.assertIn("轧差份额", alert_text)
        self.assertIn("159518", window.changed_symbols)
        self.assertEqual(window.table.item(0, 0).background().color().name(), "#fff0bd")
        self.assertFalse(window.change_banner.isHidden())
        window.reset_change_baseline()
        self.assertFalse(window.changed_symbols)
        self.assertTrue(window.change_banner.isHidden())
        self.assertEqual(window.table.item(0, 0).background().color().name(), "#000000")
        self.assertEqual(window.table.item(0, 9).text(), "0")
        self.assertEqual(window.table.item(0, 10).text(), "等待盘中变化")
        self.assertEqual(window.table.item(0, 12).text(), "—")

        # Pulling the server's unchanged full snapshot must not bring the accepted
        # opportunity/history back onto this client.
        window._apply_snapshot(
            {"type": "snapshot", "items": [changed_item], "monitoring": True}
        )
        self.assertEqual(window.table.item(0, 9).text(), "0")
        self.assertEqual(window.table.item(0, 10).text(), "等待盘中变化")
        self.assertEqual(window.table.item(0, 12).text(), "—")

        next_item = dict(changed_item)
        next_item["last_change"] = [
            {"text": "赎回份额 2,000,000 → 3,000,000"}
        ]
        next_item["opportunity"] = {
            "kind": "redemption",
            "label": "盘中赎回机会",
            "reason": "盘中净赎回增加",
        }
        window.popup_check.setChecked(False)
        window.sound_check.setChecked(False)
        window._apply_change(
            {
                "type": "change",
                "items": [
                    {
                        "symbol": "159518",
                        "current": next_item,
                        "changes": next_item["last_change"],
                    }
                ],
            }
        )
        self.assertNotIn("159518", window.baseline_suppressed_symbols)
        self.assertEqual(window.table.item(0, 10).text(), "盘中赎回机会")
        self.assertEqual(
            window.table.item(0, 12).text(), "赎回份额 2,000,000 → 3,000,000"
        )
        for popup in list(window.alert_popups):
            popup.close()
        window.close()

    def test_baskets_use_pcf_unit_and_shares_use_four_digit_grouping(self) -> None:
        item = {
            "values": {"etfbuyamount": 10_000_000, "etfsellamount": 2_500_000},
            "pcf": {"status": "ready", "creation_redemption_unit": 1_000_000},
        }
        self.assertEqual(basket_count(item, "etfbuyamount"), 10)
        self.assertEqual(format_basket_count(basket_count(item, "etfbuyamount")), "10")
        self.assertEqual(format_basket_count(basket_count(item, "etfsellamount")), "2.5")
        item["values"]["netamount"] = 7_500_000
        self.assertEqual(
            format_basket_count(basket_count(item, "netamount"), signed=True), "+7.5"
        )
        self.assertEqual(format_share_value(1_000_000), "100 0000")
        self.assertEqual(format_share_value(10_000_000), "1000 0000")
        self.assertEqual(format_share_value(-4_000_000, signed=True), "-400 0000")
        item["pcf"]["status"] = "pending"
        self.assertIsNone(basket_count(item, "etfbuyamount"))
        self.assertEqual(format_basket_count(None), "待确认")

    def test_local_creation_quota_checks_pcf_limits_and_full_baskets(self) -> None:
        # 159660 has no cumulative limit.  Its positive net limit is only
        # 0.5 basket, which is insufficient for a one-basket purchase.
        item = {
            "values": {
                "etfbuyamount": 0,
                "etfsellamount": 0,
                "netamount": 0,
            },
            "pcf": {
                "status": "ready",
                "creation_redemption_unit": 1_000_000,
                "creation_allowed": True,
                "creation_limit": 0,
                "net_creation_limit": 500_000,
            },
        }
        half_basket = classify_local_net_creation_quota(item)
        self.assertEqual(half_basket["kind"], "full")
        self.assertEqual(half_basket["label"], "0")
        self.assertEqual(half_basket["remaining_baskets"], 0)
        self.assertIn("不足一个完整申购篮子", half_basket["reason"])

        # 159518: one purchase and one redemption leave its net amount at
        # zero.  With no positive cumulative cap and a 1-basket net cap, one
        # full purchase basket is available again.
        item["pcf"].update(net_creation_limit=1_000_000)
        one_basket = classify_local_net_creation_quota(item)
        self.assertEqual(one_basket["kind"], "available")
        self.assertEqual(one_basket["label"], "1")
        self.assertEqual(one_basket["remaining_baskets"], 1)

        item["pcf"]["creation_allowed"] = None
        unknown_switch = classify_local_net_creation_quota(item)
        self.assertEqual(unknown_switch["kind"], "pending")
        self.assertEqual(unknown_switch["label"], "待确认")
        item["pcf"]["creation_allowed"] = True

        # A positive cumulative limit and a binding net limit: a redemption
        # changes the net amount and releases a whole purchase basket.
        item["pcf"].update(
            creation_redemption_unit=1_000_000,
            creation_limit=10_000_000,
            net_creation_limit=1_000_000,
        )
        item["values"].update(etfbuyamount=1_000_000, netamount=1_000_000)
        full = classify_local_net_creation_quota(item)
        self.assertEqual(full["kind"], "full")
        self.assertEqual(full["label"], "0")
        self.assertEqual(full["remaining_shares"], 0)

        # One redemption basket reduces the live net creation below the PCF
        # limit and immediately releases creation capacity.
        item["values"]["etfsellamount"] = 1_000_000
        item["values"]["netamount"] = 0
        available = classify_local_net_creation_quota(item)
        self.assertEqual(available["kind"], "available")
        self.assertEqual(available["label"], "1")
        self.assertEqual(available["remaining_shares"], 1_000_000)
        self.assertEqual(available["remaining_baskets"], 1)

        # 159866 uses a cumulative CreationLimit rather than a net limit.
        cumulative_item = {
            "values": {
                "etfbuyamount": 10_000_000,
                "etfsellamount": 0,
                "netamount": 10_000_000,
            },
            "pcf": {
                "status": "ready",
                "creation_redemption_unit": 1_000_000,
                "creation_allowed": True,
                "creation_limit": 10_000_000,
                "net_creation_limit": 0,
            },
        }
        cumulative_full = classify_local_net_creation_quota(cumulative_item)
        self.assertEqual(cumulative_full["kind"], "full")
        self.assertIn("累计上限", cumulative_full["reason"])

        # Redemption changes the net, but it does not undo cumulative creations.
        cumulative_item["values"]["etfsellamount"] = 500_000
        cumulative_item["values"]["netamount"] = 9_500_000
        still_full = classify_local_net_creation_quota(cumulative_item)
        self.assertEqual(still_full["kind"], "full")
        self.assertIn("累计上限", still_full["reason"])

        # Capacity is rendered in whole baskets.  A positive cumulative limit
        # with no positive net cap stays available and exposes the basket count.
        item["pcf"].update(creation_limit=2_000_000, net_creation_limit=0)
        item["values"].update(etfbuyamount=0, etfsellamount=0, netamount=0)
        available_without_net_cap = classify_local_net_creation_quota(item)
        self.assertEqual(available_without_net_cap["kind"], "available")
        self.assertEqual(available_without_net_cap["label"], "2")
        self.assertEqual(available_without_net_cap["remaining_baskets"], 2)

        item["pcf"].update(creation_limit=0, net_creation_limit=0)
        unlimited = classify_local_net_creation_quota(item)
        self.assertEqual(unlimited["kind"], "available")
        self.assertEqual(unlimited["label"], "不限")

        # A positive remainder smaller than one PCF unit is not actionable.
        item["pcf"]["creation_limit"] = 1_500_000
        item["values"]["etfbuyamount"] = 1_000_000
        partial = classify_local_net_creation_quota(item)
        self.assertEqual(partial["kind"], "full")
        self.assertEqual(partial["label"], "0")
        self.assertIn("不足一个完整申购篮子", partial["reason"])

        item["pcf"]["creation_allowed"] = False
        closed = classify_local_net_creation_quota(item)
        self.assertEqual(closed["kind"], "closed")
        self.assertEqual(closed["label"], "0")

    def test_creation_quota_cell_uses_high_contrast_basket_capacity(self) -> None:
        window = RemoteClientWindow()
        item = {
            "symbol": "159518",
            "status": "monitoring",
            "values": {"etfbuyamount": 0, "etfsellamount": 0, "netamount": 0},
            "pcf": {
                "status": "ready",
                "creation_redemption_unit": 1_000_000,
                "creation_allowed": True,
                "creation_limit": 2_000_000,
                "net_creation_limit": 0,
            },
        }
        window._apply_snapshot({"type": "snapshot", "items": [item]})
        cell = window.table.item(0, 9)
        self.assertEqual(cell.text(), "2")
        self.assertEqual(cell.foreground().color().name(), "#ffffff")
        self.assertEqual(cell.background().color().name(), "#166534")
        self.assertEqual(cell.font().weight(), QFont.Weight.DemiBold.value)
        window.close()

    def test_connection_settings_and_builtin_sounds_are_available(self) -> None:
        self.assertEqual(split_address("http://192.168.1.8:7000/"), ("192.168.1.8", 7000))
        self.assertEqual(split_address("monitor-host"), ("monitor-host", 6787))
        for filename in ("bright.wav", "radar.wav", "bell.wav", "urgent.wav", "soft.wav"):
            self.assertTrue(resource_path("assets", "sounds", filename).is_file())

        window = RemoteClientWindow(settings_name="TestSoundRepeatSetting")
        window.settings.remove("sound_repeat_count")
        window._restore_settings()
        self.assertEqual(window.sound_repeat_count, 3)
        self.assertEqual(window._settings_values()["sound_repeat_count"], 3)
        window.close()

    def test_qmt_code_normalization_and_protocol_state_sync(self) -> None:
        self.assertEqual(qmt_etf_code("159518"), "159518.SZ")
        self.assertEqual(qmt_etf_code("510050"), "510050.SH")
        self.assertEqual(qmt_etf_code("510050.sh"), "510050.SH")
        with self.assertRaises(ValueError):
            qmt_etf_code("15951")

        client = QmtBackendClient("QMT-test", "127.0.0.1", 9527)
        positions = [{"code": "159518.SZ", "volume": 1000000}]
        sorted_positions = _qmt_sorted_positions(positions)
        client._handle_message(
            {
                "type": "positions_data",
                "sync_mode": "full",
                "snapshot_id": "positions-1",
                "seq": 1,
                "count": 1,
                "checksum": _qmt_snapshot_checksum(
                    sorted_positions,
                    {"available_cash": 123456.78},
                ),
                "available_cash": 123456.78,
                "data": positions,
            }
        )
        initial_orders = [
            {
                "order_id": "1001",
                "code": "159518.SZ",
                "direction": "买入",
                "status": "已申报",
            }
        ]
        sorted_initial_orders = _qmt_sorted_orders(initial_orders)
        client._handle_message(
            {
                "type": "orders_data",
                "sync_mode": "full",
                "snapshot_id": "orders-1",
                "seq": 1,
                "count": 1,
                "checksum": _qmt_snapshot_checksum(sorted_initial_orders),
                "data": initial_orders,
            }
        )
        next_orders = [{"order_id": "1002", "code": "510050.SH"}]
        client._handle_message(
            {
                "type": "orders_data",
                "sync_mode": "delta",
                "snapshot_id": "orders-1",
                "seq": 2,
                "count": 1,
                "checksum": _qmt_snapshot_checksum(_qmt_sorted_orders(next_orders)),
                "upserts": next_orders,
                "remove_ids": ["1001"],
            }
        )
        self.assertEqual(client.available_cash, 123456.78)
        self.assertEqual(client.positions[0]["code"], "159518.SZ")
        self.assertEqual(client.orders_for_symbol("159518"), [])
        self.assertEqual(client.orders_for_symbol("510050")[0]["order_id"], "1002")

        with patch.object(client, "is_ready", return_value=True), patch.object(
            client, "_send_json", return_value=True
        ) as send:
            self.assertTrue(client.send_etf_order("159518", "PURCHASE"))
        request = send.call_args.args[0]
        self.assertEqual(
            {key: request[key] for key in ("type", "action", "code", "qty")},
            {"type": "etf_order", "action": "PURCHASE", "code": "159518.SZ", "qty": 1},
        )
        self.assertTrue(request["client_order_id"].startswith("ETF-QMT-test-P-159518-"))
        client.close()

    def test_pcf_detail_includes_independent_qmt_trade_tabs(self) -> None:
        window = RemoteClientWindow(settings_name="TestQmtDetailTabs")
        payload = {
            "symbol": "159518",
            "fund_name": "测试 ETF",
            "trading_day": "2026-08-12",
            "summary_fields": [],
            "component_columns": [],
            "components": [],
        }
        window._show_pcf_detail(payload)
        dialog = window.pcf_dialogs[0]
        self.assertEqual(set(dialog.qmt_tabs), {"QMT1", "QMT2"})
        tabs = dialog.findChild(QTabWidget, "pcfDetailTabs")
        self.assertIsNotNone(tabs)
        self.assertEqual([tabs.tabText(index) for index in range(tabs.count())], ["清单摘要", "成分证券（0）", "QMT1", "QMT2"])
        rendered_labels = "\n".join(label.text() for label in dialog.findChildren(QLabel))
        self.assertNotIn("判断：", rendered_labels)
        self.assertNotIn("当前标的仅发送", rendered_labels)
        self.assertEqual(
            len(dialog.findChildren(DoubleClickButton)),
            4,
        )
        purchase = dialog.qmt_tabs["QMT1"]["purchase"]
        redeem = dialog.qmt_tabs["QMT1"]["redeem"]
        self.assertGreaterEqual(purchase.minimumWidth(), 272)
        self.assertGreaterEqual(purchase.minimumHeight(), 68)
        self.assertGreaterEqual(redeem.minimumWidth(), 272)
        self.assertIn("QTabWidget#pcfDetailTabs", dialog.styleSheet())
        qmt1 = window.qmt_clients["QMT1"]
        positions: list[dict] = []
        qmt1._handle_message(
            {
                "type": "positions_data",
                "sync_mode": "full",
                "snapshot_id": "positions-1",
                "seq": 1,
                "count": 0,
                "checksum": _qmt_snapshot_checksum(
                    positions,
                    {"available_cash": 10000},
                ),
                "available_cash": 10000,
                "data": positions,
            }
        )
        self.assertIn("¥10,000.00", dialog.qmt_tabs["QMT1"]["cash"].text())
        with patch.object(qmt1, "is_ready", return_value=True), patch.object(
            qmt1, "send_etf_order", return_value=True
        ) as send:
            dialog._submit_qmt_order("QMT1", "REDEEM")
        send.assert_called_once_with("159518", "REDEEM")
        dialog.close()
        window.close()

    def test_qmt_requires_welcome_and_two_valid_full_snapshots_before_ready(self) -> None:
        client = QmtBackendClient("QMT-test", "127.0.0.1", 9527)
        client.connection_state = "syncing"
        with patch.object(client, "is_connected", return_value=True):
            client._handle_message({"type": "welcome", "push_sync": True})
            self.assertFalse(client.is_ready())

            orders: list[dict] = []
            client._handle_message(
                {
                    "type": "orders_data",
                    "sync_mode": "full",
                    "snapshot_id": "orders-1",
                    "seq": 1,
                    "count": 0,
                    "checksum": _qmt_snapshot_checksum(orders),
                    "data": orders,
                }
            )
            self.assertFalse(client.is_ready())

            positions: list[dict] = []
            client._handle_message(
                {
                    "type": "positions_data",
                    "sync_mode": "full",
                    "snapshot_id": "positions-1",
                    "seq": 1,
                    "count": 0,
                    "checksum": _qmt_snapshot_checksum(
                        positions,
                        {"available_cash": 10000.0},
                    ),
                    "available_cash": 10000.0,
                    "data": positions,
                }
            )
            self.assertTrue(client.is_ready())
            self.assertEqual(client.status_text(), "已连接")
        client.close()

    def test_qmt_rejects_bad_delta_and_requests_orders_full_sync(self) -> None:
        client = QmtBackendClient("QMT-test", "127.0.0.1", 9527)
        initial_orders = [{"order_id": "1001", "code": "159518.SZ"}]
        client._handle_message(
            {
                "type": "orders_data",
                "sync_mode": "full",
                "snapshot_id": "orders-1",
                "seq": 7,
                "count": 1,
                "checksum": _qmt_snapshot_checksum(_qmt_sorted_orders(initial_orders)),
                "data": initial_orders,
            }
        )
        with patch.object(client, "is_connected", return_value=True), patch.object(
            client, "_send_json", return_value=True
        ) as send:
            client._handle_message(
                {
                    "type": "orders_data",
                    "sync_mode": "delta",
                    "snapshot_id": "wrong-snapshot",
                    "seq": 999,
                    "count": 2,
                    "checksum": "bad",
                    "upserts": [{"order_id": "unexpected", "code": "159518.SZ"}],
                    "remove_ids": [],
                }
            )
        self.assertEqual(set(client.orders_by_id), {"1001"})
        self.assertFalse(client._orders_synced)
        self.assertIn("序号不连续", client.last_error)
        send.assert_called_once_with({"type": "sync_request", "target": "orders"})
        client.close()

    def test_qmt_etf_orders_are_throttled_for_five_seconds(self) -> None:
        client = QmtBackendClient("QMT-test", "127.0.0.1", 9527)
        with patch.object(client, "is_ready", return_value=True), patch.object(
            client, "_send_json", return_value=True
        ) as send, patch("etf_remote_client.time.monotonic", return_value=100.0):
            self.assertTrue(client.send_etf_order("159518", "PURCHASE"))
            self.assertFalse(client.send_etf_order("159518", "REDEEM"))
        self.assertEqual(send.call_count, 1)
        self.assertIn("操作过快", client.last_error)

        with patch.object(client, "is_ready", return_value=True), patch.object(
            client, "_send_json", return_value=True
        ) as send, patch("etf_remote_client.time.monotonic", return_value=105.01):
            self.assertTrue(client.send_etf_order("159518", "REDEEM"))
        send.assert_called_once()
        client.close()

    def test_qmt_heartbeat_marks_silent_backend_for_reconnect(self) -> None:
        client = QmtBackendClient("QMT-test", "127.0.0.1", 9527)
        client.want_connection = True
        client.connection_state = "ready"
        client._last_backend_message_at = 1.0
        with patch.object(client, "is_connected", return_value=True), patch(
            "etf_remote_client.time.monotonic", return_value=17.0
        ):
            client._heartbeat_tick()
        self.assertEqual(client.connection_state, "reconnecting")
        self.assertIn("无响应", client.last_error)
        client.reconnect_timer.stop()
        client.close()

    def test_web_client_has_local_net_creation_quota_column(self) -> None:
        source_html = resource_path("web", "monitor.html").read_text(encoding="utf-8")
        self.assertIn("可申购篮子", source_html)
        self.assertIn("function creationQuota(item)", source_html)
        self.assertIn("申购篮子", source_html)
        self.assertIn("轧差篮子", source_html)
        self.assertIn("creation_redemption_unit", source_html)
        self.assertIn("PCF 未设置正数累计申购或净申购上限", source_html)
        self.assertIn("quota-available", source_html)
        self.assertIn("quota-unavailable", source_html)
        self.assertNotIn("只读客户端 · 观察列表", source_html)

    def test_connection_button_uses_manual_red_and_connected_green_states(self) -> None:
        window = RemoteClientWindow(settings_name="TestConnectionButton")
        self.assertFalse(window.server_controls)
        self.assertTrue(window.address_input.isHidden())
        self.assertTrue(window.watchlist_card.isHidden())
        self.assertIsNone(window.remote_start_button.parent())
        self.assertIsNone(window.remote_stop_button.parent())
        self.assertIsNone(window.pcf_refresh_button.parent())
        self.assertIsNone(window.add_symbol_button.parent())
        self.assertIsNone(window.remove_symbol_button.parent())
        self.assertFalse(window.auto_connect_enabled)
        self.assertEqual(window.connect_button.property("connection_state"), "disconnected")
        self.assertIn("#c53b45", window.connect_button.styleSheet())
        window._on_connected()
        self.assertEqual(window.connect_button.property("connection_state"), "connected")
        self.assertIn("#168553", window.connect_button.styleSheet())
        window._on_disconnected()
        self.assertEqual(window.connect_button.property("connection_state"), "disconnected")
        window.close()

    def test_alert_cooldown_does_not_block_table_updates(self) -> None:
        window = RemoteClientWindow()
        window.alert_cooldown_seconds = 60
        window.last_alert_at = 100.0
        event = {
            "type": "change",
            "items": [{"symbol": "159518", "current": {"symbol": "159518"}, "changes": [{"text": "变化"}]}],
        }
        with patch("etf_remote_client.time.monotonic", return_value=101.0), patch.object(
            window, "play_alert_sound"
        ) as play_sound:
            window._apply_change(event)
        self.assertIn("159518", window.items)
        play_sound.assert_not_called()
        self.assertIn("冷却中", window.footer_label.text())
        window.close()

    def test_pcf_detail_dialog_renders_summary_and_components(self) -> None:
        window = RemoteClientWindow()
        payload = {
            "symbol": "159518",
            "fund_name": "测试 ETF",
            "trading_day": "2026-08-11",
            "opportunity": {"kind": "creation", "label": "申购机会", "reason": "1 篮子"},
            "summary_fields": [{"field": "CreationRedemptionUnit", "label": "最小申赎单位", "value": "1000000"}],
            "component_columns": [{"field": "UnderlyingSecurityID", "label": "证券代码"}],
            "components": [{"UnderlyingSecurityID": "XOP"}],
        }
        window._show_pcf_detail(payload)
        self.assertEqual(len(window.pcf_dialogs), 1)
        self.assertIn("159518", window.pcf_dialogs[0].windowTitle())
        self.assertFalse(hasattr(window.pcf_dialogs[0], "name_input"))
        window.pcf_dialogs[0].close()
        window.close()

    def test_host_pcf_detail_can_save_custom_name(self) -> None:
        window = RemoteClientWindow(server_controls=True)
        payload = {
            "symbol": "159866",
            "name": "日经 ETF",
            "custom_name": "",
            "fund_name": "日经ETF工银",
            "trading_day": "2026-08-12",
            "summary_fields": [],
            "component_columns": [],
            "components": [],
        }
        with patch.object(window, "_request") as request:
            window._show_pcf_detail(payload)
            dialog = window.pcf_dialogs[0]
            dialog.name_input.setText("日经套利")
            dialog.name_save_button.click()
            request.assert_called_once_with(
                "PUT",
                "/api/v1/symbols/159866/name",
                {"name": "日经套利"},
                request_kind="symbol-name",
            )
        dialog.close()
        window.close()


if __name__ == "__main__":
    unittest.main()
