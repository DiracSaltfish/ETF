#!/usr/bin/env python3
"""Mac-home desktop host: local UI plus embedded LAN/Web monitor service."""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import uvicorn
from PyQt6.QtCore import QThread, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QDesktopServices, QFont
from PyQt6.QtNetwork import QLocalServer, QLocalSocket, QNetworkRequest
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QStyle,
    QSystemTrayIcon,
    QTextEdit,
    QVBoxLayout,
)

from etf_monitor_server import ConfigStore, configure_file_logging, create_app
from etf_remote_client import RemoteClientWindow


HOST_DATA_DIR = Path.home() / "Library" / "Application Support" / "ETFDelivery"
HOST_CONFIG_PATH = HOST_DATA_DIR / "config" / "etf_monitor_server.json"
HOST_LOG_PATH = HOST_DATA_DIR / "logs" / "server.log"
LOCAL_SERVER_NAME = "com.etfdelivery.mac-home-ui"


def local_ip_address() -> str:
    """Return the preferred LAN address without sending any network packet."""

    for interface in ("en0", "en1"):
        try:
            result = subprocess.run(
                ["/usr/sbin/ipconfig", "getifaddr", interface],
                text=True,
                capture_output=True,
                timeout=1,
            )
            value = result.stdout.strip()
            if result.returncode == 0 and value and not value.startswith("127."):
                return value
        except (OSError, subprocess.TimeoutExpired):
            pass
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return str(sock.getsockname()[0])
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"
    finally:
        sock.close()


def tcp_port_open(host: str, port: int) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.35):
            return True
    except OSError:
        return False


class EmbeddedServerThread(QThread):
    ready = pyqtSignal(str, int)
    failed = pyqtSignal(str)
    stopped = pyqtSignal()

    def __init__(self, config_path: Path = HOST_CONFIG_PATH) -> None:
        super().__init__()
        self.config_path = config_path
        self.server: uvicorn.Server | None = None

    def run(self) -> None:
        try:
            try:
                configure_file_logging(HOST_LOG_PATH)
            except OSError:
                # Useful for portable/test launches where Application Support
                # is read-only; production normally uses HOST_LOG_PATH.
                configure_file_logging(self.config_path.parent / "server.log")
            config = ConfigStore(self.config_path)
            host = str(config.data["network"].get("host", "0.0.0.0"))
            port = int(config.data["network"].get("port", 6787))
            application = create_app(self.config_path)
            uvicorn_config = uvicorn.Config(
                application,
                host=host,
                port=port,
                log_level="info",
                access_log=False,
            )
            self.server = uvicorn.Server(uvicorn_config)
            asyncio.run(self._serve(host, port))
        except BaseException as exc:  # thread boundary; surface the full failure
            self.failed.emit(str(exc))
        finally:
            self.stopped.emit()

    async def _serve(self, host: str, port: int) -> None:
        assert self.server is not None
        task = asyncio.create_task(self.server.serve())
        while not self.server.started and not task.done():
            await asyncio.sleep(0.05)
        if self.server.started:
            self.ready.emit(host, port)
        await task

    def request_stop(self) -> None:
        if self.server is not None:
            self.server.should_exit = True


def notify_existing_instance() -> bool:
    client = QLocalSocket()
    client.connectToServer(LOCAL_SERVER_NAME)
    if not client.waitForConnected(350):
        return False
    client.write(b"show")
    client.waitForBytesWritten(350)
    client.disconnectFromServer()
    return True


def install_single_instance_listener(window: "MacHomeWindow") -> QLocalServer:
    server = QLocalServer(window)
    QLocalServer.removeServer(LOCAL_SERVER_NAME)
    if not server.listen(LOCAL_SERVER_NAME):
        raise RuntimeError(f"无法建立单实例通道：{server.errorString()}")

    def show_requested() -> None:
        while server.hasPendingConnections():
            connection = server.nextPendingConnection()
            if connection is not None:
                connection.close()
        window.show_from_tray()

    server.newConnection.connect(show_requested)
    return server


class MacHomeWindow(RemoteClientWindow):
    def __init__(
        self,
        *,
        config_path: Path = HOST_CONFIG_PATH,
        autostart_server: bool = True,
    ) -> None:
        super().__init__(
            settings_name="ETFMacHome",
            window_title="ETF 监控主机",
            default_address="127.0.0.1:6787",
            server_editable=False,
            server_controls=True,
        )
        self.config_path = config_path
        self.server_thread: EmbeddedServerThread | None = None
        self.owns_server = False
        self.allow_quit = False
        self.log_dialog: QDialog | None = None
        self.log_view: QTextEdit | None = None
        self.log_timer: QTimer | None = None
        self._close_notice_shown = False

        self.address_input.setText("127.0.0.1:6787")
        self.address_input.setReadOnly(True)
        self.connect_button.hide()
        self.disconnect_button.hide()
        self._insert_host_card()
        self._create_tray_icon()
        if autostart_server:
            QTimer.singleShot(0, self.start_host_service)

    def _insert_host_card(self) -> None:
        card = QFrame()
        card.setObjectName("card")
        row = QHBoxLayout(card)
        row.setContentsMargins(16, 12, 16, 12)
        self.service_label = QLabel("● 本机服务准备中")
        row.addWidget(self.service_label)
        row.addSpacing(12)
        self.wind_status_label = QLabel("● 正在检测 Wind")
        self.wind_status_label.setStyleSheet("color: #68758a;")
        row.addWidget(self.wind_status_label)
        row.addSpacing(12)
        self.lan_url = f"http://{local_ip_address()}:6787/"
        self.lan_label = QLabel(f"内网页面：{self.lan_url}")
        self.lan_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        row.addWidget(self.lan_label)
        row.addStretch()
        self.open_web_button = QPushButton("打开网页")
        self.open_web_button.clicked.connect(self.open_web_page)
        self.log_button = QPushButton("服务日志")
        self.log_button.clicked.connect(self.show_log_dialog)
        self.start_wind_button = QPushButton("启动 Wind")
        self.start_wind_button.clicked.connect(self.start_wind_from_ui)
        self.shutdown_wind_button = QPushButton("关闭 Wind 并清理")
        self.shutdown_wind_button.clicked.connect(self.shutdown_wind_from_ui)
        self.start_wind_button.setEnabled(False)
        self.shutdown_wind_button.setEnabled(False)
        row.addWidget(self.start_wind_button)
        row.addWidget(self.shutdown_wind_button)
        row.addWidget(self.open_web_button)
        row.addWidget(self.log_button)
        self.root_layout.insertWidget(0, card)

    def _apply_wind_state(self, value: Any) -> None:
        if not isinstance(value, dict):
            return
        state = str(value.get("state") or "unknown")
        label = str(value.get("label") or "Wind 状态未知")
        running = bool(value.get("running"))
        busy = state in {"launching", "subscribing", "stopping", "quitting", "cleaning"}
        if state in {"ready", "monitoring"}:
            color = "#168553"
        elif state in {"launching", "subscribing", "stopping", "quitting", "cleaning", "running"}:
            color = "#d97706"
        elif state == "error":
            color = "#c53b45"
        else:
            color = "#68758a"
        self.wind_status_label.setText(f"● {label}")
        self.wind_status_label.setStyleSheet(f"color: {color};")
        details = [str(value.get("last_action") or "")]
        if value.get("last_error"):
            details.append(f"错误：{value['last_error']}")
        if value.get("cleanup_deleted_count") is not None:
            details.append(f"最近清理 {int(value.get('cleanup_deleted_count') or 0)} 个 dylib")
        self.wind_status_label.setToolTip("\n".join(item for item in details if item))
        self.start_wind_button.setEnabled(not busy and not running)
        self.shutdown_wind_button.setEnabled(not busy)
        self.remote_start_button.setEnabled(not busy and running)
        self.remote_stop_button.setEnabled(not busy and running)

    def _apply_snapshot(self, event: dict[str, Any]) -> None:
        super()._apply_snapshot(event)
        self._apply_wind_state(event.get("wind"))

    def _on_text_message(self, text: str) -> None:
        super()._on_text_message(text)
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return
        self._apply_wind_state(event.get("wind"))

    def start_wind_from_ui(self) -> None:
        self.wind_status_label.setText("● 正在请求启动 Wind")
        self.start_wind_button.setEnabled(False)
        self.shutdown_wind_button.setEnabled(False)
        self._request(
            "POST", "/api/v1/wind/start", request_kind="wind-action"
        )

    def shutdown_wind_from_ui(self) -> None:
        answer = QMessageBox.question(
            self,
            "关闭 Wind 并清理",
            "将先停止全部 Wind 订阅，再关闭 Wind；仅在确认进程完全退出后清理临时 dylib。是否继续？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.wind_status_label.setText("● 正在请求关闭 Wind")
        self.start_wind_button.setEnabled(False)
        self.shutdown_wind_button.setEnabled(False)
        self._request(
            "POST",
            "/api/v1/wind/shutdown-cleanup",
            request_kind="wind-action",
        )

    def _http_finished(self, reply: Any) -> None:
        request_kind = str(reply.property("request_kind") or "")
        status = reply.attribute(
            QNetworkRequest.Attribute.HttpStatusCodeAttribute
        )
        super()._http_finished(reply)
        if request_kind != "wind-action":
            return
        if status is None or int(status) >= 400:
            self._apply_wind_state(
                {
                    "state": "error",
                    "label": "Wind 控制请求失败",
                    "running": False,
                    "last_error": "请查看页面底部或服务日志",
                }
            )
            if status is not None:
                QTimer.singleShot(
                    0,
                    lambda: self._request(
                        "GET", "/api/v1/wind/status", request_kind="wind-status"
                    ),
                )

    def _create_tray_icon(self) -> None:
        icon = self.style().standardIcon(QStyle.StandardPixmap.SP_ComputerIcon)
        self.tray = QSystemTrayIcon(icon, self)
        self.tray.setToolTip("ETF 监控主机")
        menu = QMenu()
        show_action = QAction("显示主界面", self)
        show_action.triggered.connect(self.show_from_tray)
        web_action = QAction("打开内网页面", self)
        web_action.triggered.connect(self.open_web_page)
        quit_action = QAction("退出监控主机", self)
        quit_action.triggered.connect(self.quit_completely)
        menu.addAction(show_action)
        menu.addAction(web_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self._tray_activated)
        self.tray.show()

    def _tray_activated(self, reason: Any) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.show_from_tray()

    def show_from_tray(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def start_host_service(self) -> None:
        config = ConfigStore(self.config_path)
        port = int(config.data["network"].get("port", 6787))
        self.address_input.setText(f"127.0.0.1:{port}")
        self.lan_url = f"http://{local_ip_address()}:{port}/"
        self.lan_label.setText(f"内网页面：{self.lan_url}")
        if tcp_port_open("127.0.0.1", port):
            self.service_label.setText("● 已连接现有本机服务")
            self.service_label.setStyleSheet("color: #168553;")
            QTimer.singleShot(0, self.connect_server)
            return
        self.server_thread = EmbeddedServerThread(self.config_path)
        self.server_thread.ready.connect(self._server_ready)
        self.server_thread.failed.connect(self._server_failed)
        self.server_thread.stopped.connect(self._server_stopped)
        self.server_thread.start()

    def _server_ready(self, _host: str, _port: int) -> None:
        self.owns_server = True
        self.service_label.setText("● 本机服务运行中")
        self.service_label.setStyleSheet("color: #168553;")
        self.connect_server()

    def _server_failed(self, message: str) -> None:
        # A service may have won a startup race for the port.  Prefer using it
        # over showing a false fatal error.
        if tcp_port_open("127.0.0.1", int(self._host_port().rsplit(":", 1)[-1])):
            self.service_label.setText("● 已连接现有本机服务")
            self.service_label.setStyleSheet("color: #168553;")
            self.connect_server()
            return
        self.service_label.setText("● 本机服务启动失败")
        self.service_label.setStyleSheet("color: #c53b45;")
        self.footer_label.setText(f"服务启动失败：{message}")

    def _server_stopped(self) -> None:
        if self.allow_quit:
            return
        self.owns_server = False
        self.service_label.setText("● 本机服务已停止")
        self.service_label.setStyleSheet("color: #c53b45;")

    def open_web_page(self) -> None:
        QDesktopServices.openUrl(QUrl(self.lan_url))

    def show_log_dialog(self) -> None:
        if self.log_dialog is None:
            self.log_dialog = QDialog(self)
            self.log_dialog.setWindowTitle("服务日志")
            self.log_dialog.resize(900, 520)
            layout = QVBoxLayout(self.log_dialog)
            self.log_view = QTextEdit()
            self.log_view.setReadOnly(True)
            layout.addWidget(self.log_view)
            close_button = QPushButton("关闭")
            close_button.clicked.connect(self.log_dialog.close)
            layout.addWidget(close_button)
            self.log_timer = QTimer(self.log_dialog)
            self.log_timer.setInterval(2000)
            self.log_timer.timeout.connect(self._refresh_log)
            self.log_timer.start()
        self._refresh_log()
        self.log_dialog.show()
        self.log_dialog.raise_()

    def _refresh_log(self) -> None:
        if self.log_view is None:
            return
        try:
            lines = HOST_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
            self.log_view.setPlainText("\n".join(lines[-500:]))
            cursor = self.log_view.textCursor()
            cursor.movePosition(cursor.MoveOperation.End)
            self.log_view.setTextCursor(cursor)
        except OSError:
            self.log_view.setPlainText(f"日志尚未生成：{HOST_LOG_PATH}")

    def closeEvent(self, event: Any) -> None:
        if self.allow_quit:
            super().closeEvent(event)
            return
        event.ignore()
        self.hide()
        if not self._close_notice_shown:
            self.tray.showMessage(
                "ETF 监控主机仍在运行",
                "主界面已收起，可从菜单栏图标重新打开。",
                QSystemTrayIcon.MessageIcon.Information,
                3500,
            )
            self._close_notice_shown = True

    def _server_shutdown_timeout_ms(self) -> int:
        config = ConfigStore(self.config_path)
        symbol_count = max(1, len(config.symbols))
        wind = config.data.get("wind", {})
        if not isinstance(wind, dict):
            wind = {}
        # ProbeController.stop_many uses max(45, 20 + symbols * 8) seconds.
        # Add the configured Wind termination timeout and a bounded cleanup/
        # event-loop margin so the GUI cannot truncate the server's safe exit.
        stop_seconds = max(45.0, 20.0 + symbol_count * 8.0)
        terminate_seconds = max(
            1.0, float(wind.get("terminate_timeout_seconds", 30))
        )
        return int((stop_seconds + terminate_seconds + 30.0) * 1000)

    def stop_owned_server(self) -> bool:
        if self.server_thread is None or not self.server_thread.isRunning():
            return True
        self.server_thread.request_stop()
        if self.server_thread.wait(self._server_shutdown_timeout_ms()):
            return True
        self.footer_label.setText(
            "服务仍在执行安全停订/关闭/清理，已取消退出，请稍后重试。"
        )
        self.tray.showMessage(
            "ETF 监控主机尚未退出",
            "正在等待 Wind 安全关闭与清理，请稍后再次选择退出。",
            QSystemTrayIcon.MessageIcon.Warning,
            5000,
        )
        return False

    def quit_completely(self) -> None:
        self.allow_quit = True
        self.want_connection = False
        self._save_settings()
        self.socket.close()
        if not self.stop_owned_server():
            self.allow_quit = False
            self.show_from_tray()
            return
        self.tray.hide()
        QApplication.quit()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--background", action="store_true", help="start with window hidden")
    parser.add_argument("--config", type=Path, default=HOST_CONFIG_PATH)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    app = QApplication(sys.argv[:1])
    app.setApplicationName("ETF 监控主机")
    app.setOrganizationName("ETFDelivery")
    app.setQuitOnLastWindowClosed(False)
    if notify_existing_instance():
        return 0
    font = QFont()
    font.setPointSize(13)
    app.setFont(font)
    window = MacHomeWindow(config_path=args.config)
    window.single_instance_server = install_single_instance_listener(window)
    if not args.background:
        window.show()
    app.aboutToQuit.connect(window.stop_owned_server)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
