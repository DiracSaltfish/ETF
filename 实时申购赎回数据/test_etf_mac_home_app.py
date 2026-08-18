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
            self.assertEqual(window.start_wind_button.text(), "启动 Wind")
            self.assertEqual(window.shutdown_wind_button.text(), "关闭 Wind 并清理")
            window._apply_wind_state(
                {"state": "ready", "label": "Wind 已就绪", "running": True}
            )
            self.assertIn("Wind 已就绪", window.wind_status_label.text())
            self.assertFalse(window.start_wind_button.isEnabled())
            self.assertTrue(window.shutdown_wind_button.isEnabled())
            self.assertGreater(window._server_shutdown_timeout_ms(), 60_000)
            self.assertTrue(local_ip_address())

            class FailedWindReply:
                def property(self, name):
                    return "wind-action" if name == "request_kind" else ""

                def attribute(self, _name):
                    return None

                def readAll(self):
                    return b""

                def deleteLater(self):
                    return None

            window.start_wind_button.setEnabled(False)
            window.shutdown_wind_button.setEnabled(False)
            window._http_finished(FailedWindReply())
            self.assertTrue(window.start_wind_button.isEnabled())
            self.assertTrue(window.shutdown_wind_button.isEnabled())
            window.allow_quit = True
            window.close()


if __name__ == "__main__":
    unittest.main()
