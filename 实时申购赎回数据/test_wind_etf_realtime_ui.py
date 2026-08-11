#!/usr/bin/env python3

import json
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from wind_etf_realtime_ui import MainWindow, SYMBOL_PATTERN, normalize_symbol


HERE = Path(__file__).resolve().parent
APP = QApplication.instance() or QApplication([])


class WindEtfRealtimeUiTest(unittest.TestCase):
    def test_symbol_validation(self) -> None:
        self.assertIsNotNone(SYMBOL_PATTERN.fullmatch("159518"))
        self.assertIsNotNone(SYMBOL_PATTERN.fullmatch("159393"))
        self.assertIsNone(SYMBOL_PATTERN.fullmatch("159518.SZ"))
        self.assertIsNone(SYMBOL_PATTERN.fullmatch("159518;quit"))
        self.assertEqual(normalize_symbol("159518"), "159518.SZ")

    def test_renders_capture_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            capture_dir = Path(directory)
            with (HERE / "sample_wind_tbapi_probe_sub_1.json").open(
                "r", encoding="utf-8"
            ) as handle:
                capture = json.load(handle)
            live_path = capture_dir / "wind_tbapi_live_159518_SZ.json"
            with live_path.open("w", encoding="utf-8") as handle:
                json.dump(capture, handle)

            window = MainWindow(
                capture_dir=capture_dir,
                log_path=capture_dir / "ui.log",
            )
            window.symbol_input.setText("159518")
            window.refresh_data()
            self.assertEqual(window.table.item(0, 0).text(), "3")
            self.assertEqual(window.table.item(0, 1).text(), "2")
            self.assertEqual(window.table.item(1, 0).text(), "0")
            self.assertEqual(window.table.item(2, 0).text(), "3,000,000  (300万)")
            self.assertEqual(window.table.item(2, 1).text(), "2,000,000  (200万)")
            self.assertEqual(window.windowTitle(), "ETF 实时数据")
            self.assertNotIn("TBAPI2", window.detail_label.text())
            self.assertNotIn("wind_tbapi", window.detail_label.text())
            self.assertTrue((capture_dir / "ui.log").exists())
            window.refresh_timer.stop()
            window.close()


if __name__ == "__main__":
    unittest.main()
