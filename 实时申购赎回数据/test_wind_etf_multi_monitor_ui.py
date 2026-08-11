#!/usr/bin/env python3

import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from wind_etf_multi_monitor_ui import MonitorWindow


HERE = Path(__file__).resolve().parent
APP = QApplication.instance() or QApplication([])


def make_capture(
    windcode: str,
    buy_number: int,
    buy_amount: int,
    buy_money: int,
    sell_number: int,
    sell_amount: int,
    sell_money: int,
) -> dict:
    with (HERE / "sample_wind_tbapi_probe_sub_1.json").open(
        "r", encoding="utf-8"
    ) as handle:
        capture = json.load(handle)
    raw = bytearray.fromhex(capture["buffer_58"]["hex"])
    base = 12
    struct.pack_into("<i", raw, base + 0, buy_number)
    struct.pack_into("<q", raw, base + 4, buy_amount)
    struct.pack_into("<q", raw, base + 12, buy_money)
    struct.pack_into("<i", raw, base + 20, sell_number)
    struct.pack_into("<q", raw, base + 24, sell_amount)
    struct.pack_into("<q", raw, base + 32, sell_money)
    code_bytes = windcode.encode("ascii")
    raw[base + 40 : base + 104] = code_bytes + bytes(64 - len(code_bytes))
    capture["buffer_58"]["hex"] = raw.hex()
    capture["requested_windcode"] = windcode
    capture["callback_epoch_ms"] = int(time.time() * 1000)
    return capture


class MultiMonitorUiTest(unittest.TestCase):
    def test_two_symbol_baseline_and_change_alert(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = make_capture("159518.SZ", 3, 3_000_000, 0, 2, 2_000_000, 0)
            second = make_capture("159393.SZ", 0, 0, 0, 1, 900_000, 0)
            (root / "wind_tbapi_live_159518_SZ.json").write_text(
                json.dumps(first), encoding="utf-8"
            )
            second_path = root / "wind_tbapi_live_159393_SZ.json"
            second_path.write_text(json.dumps(second), encoding="utf-8")

            window = MonitorWindow(capture_dir=root, log_path=root / "ui.log")
            window.symbols = ["159518.SZ", "159393.SZ"]
            window._rebuild_table()
            window.popup_check.setChecked(True)
            window.sound_check.setChecked(True)
            window.refresh_data()
            self.assertEqual(window.last_alert_messages, [])
            self.assertEqual(window.table.item(0, 3).text(), "3,000,000 (300万)")
            self.assertEqual(window.table.item(0, 6).text(), "+1,000,000 (+100万)")
            self.assertEqual(window.table.item(1, 5).text(), "900,000 (90万)")
            self.assertEqual(window.table.item(1, 6).text(), "-900,000 (-90万)")

            changed = make_capture("159393.SZ", 0, 0, 0, 2, 1_800_000, 0)
            second_path.write_text(json.dumps(changed), encoding="utf-8")
            with patch("wind_etf_multi_monitor_ui.QApplication.beep") as beep:
                window.refresh_data()
                beep.assert_called_once()
            self.assertEqual(len(window.last_alert_messages), 1)
            self.assertIn("159393", window.last_alert_messages[0])
            self.assertIn("赎回份额", window.last_alert_messages[0])
            self.assertNotIn("轧差份额", window.last_alert_messages[0])
            self.assertNotIn("赎回笔数", window.last_alert_messages[0])
            self.assertNotIn("轧差份额", window.table.item(1, 8).text())
            self.assertEqual(
                window.table.item(1, 6).background().color().name(), "#ffff00"
            )
            self.assertEqual(window.table.item(1, 6).text(), "-1,800,000 (-180万)")
            self.assertEqual(len(window.alert_popups), 1)
            self.assertEqual(window.alert_popups[0].windowTitle(), "数据变化提醒")
            for popup in list(window.alert_popups):
                popup.close()
            window.refresh_timer.stop()
            window.close()


if __name__ == "__main__":
    unittest.main()
