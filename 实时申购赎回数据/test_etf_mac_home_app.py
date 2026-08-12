#!/usr/bin/env python3

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from etf_mac_home_app import MacHomeWindow, local_ip_address


APP = QApplication.instance() or QApplication([])


class MacHomeAppTest(unittest.TestCase):
    def test_host_ui_exposes_local_service_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            window = MacHomeWindow(
                config_path=Path(directory) / "config.json",
                autostart_server=False,
            )
            self.assertEqual(window.windowTitle(), "ETF 监控主机")
            self.assertTrue(window.address_input.isReadOnly())
            self.assertIn(":6787/", window.lan_url)
            self.assertEqual(window.table.columnCount(), 13)
            self.assertFalse(window.watchlist_card.isHidden())
            self.assertTrue(local_ip_address())
            window.allow_quit = True
            window.close()


if __name__ == "__main__":
    unittest.main()
