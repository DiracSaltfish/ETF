#!/usr/bin/env python3
"""Cross-platform PyQt6 client for the ETF monitor server."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QObject, QSettings, Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QFont
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtNetwork import (
    QAbstractSocket,
    QNetworkAccessManager,
    QNetworkReply,
    QNetworkRequest,
    QTcpSocket,
)
from PyQt6.QtWebSockets import QWebSocket
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


PRESET_SOUNDS = (
    ("bright", "轻亮三音", "bright.wav"),
    ("radar", "雷达脉冲", "radar.wav"),
    ("bell", "双音铃声", "bell.wav"),
    ("urgent", "紧急三连音", "urgent.wav"),
    ("soft", "柔和提示", "soft.wav"),
    ("external", "外部音频文件", ""),
)

QMT_BACKENDS: dict[str, tuple[str, int]] = {
    "QMT1": ("192.168.1.112", 9527),
    "QMT2": ("192.168.1.111", 9527),
}
QMT_ORDER_THROTTLE_SECONDS = 5.0
QMT_HEARTBEAT_INTERVAL_MS = 5_000
QMT_BACKEND_SILENCE_SECONDS = 15.0


def qmt_etf_code(symbol: str) -> str:
    """Return a six-digit ETF symbol in the QMT ``code.exchange`` format."""

    clean = str(symbol or "").strip().upper()
    if "." in clean:
        code, exchange = clean.rsplit(".", 1)
        if len(code) == 6 and code.isdigit() and exchange in {"SH", "SZ"}:
            return f"{code}.{exchange}"
    if len(clean) != 6 or not clean.isdigit():
        raise ValueError("ETF 代码必须是 6 位数字或标准 QMT 代码")
    # ETF 5/6/9 段在上海，其余常用 ETF 代码（15/16/18）在深圳。
    exchange = "SH" if clean.startswith(("5", "6", "9")) else "SZ"
    return f"{clean}.{exchange}"


class DoubleClickButton(QPushButton):
    """A visible action button whose connected action only fires on double click."""

    doubleClicked = pyqtSignal()

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.doubleClicked.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


def _qmt_snapshot_checksum(data: list[dict[str, Any]], extra: dict[str, Any] | None = None) -> str:
    payload = {"data": data, "extra": extra or {}}
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha1(serialized.encode("utf-8")).hexdigest()


def _qmt_order_time_sort_key(value: Any) -> tuple[int, int, int]:
    text = str(value or "").strip()
    if not text:
        return (0, 0, 0)
    match = re.search(r"(\d{1,2}):(\d{2}):(\d{2})(?:[.,](\d+))?", text)
    if match:
        hour_text, minute_text, second_text, fraction_text = match.groups()
    else:
        digits = "".join(character for character in text if character.isdigit())
        if len(digits) >= 14:
            hour_text, minute_text, second_text = digits[-6:-4], digits[-4:-2], digits[-2:]
            fraction_text = ""
        elif len(digits) >= 6:
            hour_text, minute_text, second_text = digits[:2], digits[2:4], digits[4:6]
            fraction_text = digits[6:]
        else:
            return (0, 0, 0)
    try:
        hour, minute, second = int(hour_text), int(minute_text), int(second_text)
    except (TypeError, ValueError):
        return (0, 0, 0)
    if hour > 23 or minute > 59 or second > 59:
        return (0, 0, 0)
    fraction_digits = "".join(
        character for character in str(fraction_text or "") if character.isdigit()
    )[:6]
    fraction = int(fraction_digits.ljust(6, "0")) if fraction_digits else 0
    return (1, hour * 3600 + minute * 60 + second, fraction)


def _qmt_order_sort_key(order: dict[str, Any]) -> tuple[Any, ...]:
    order_id = str(order.get("order_id") or "").strip()
    numeric_order_id = order_id.isdigit()
    try:
        sort_sequence = int(order.get("sort_seq") or 0)
    except (TypeError, ValueError):
        sort_sequence = 0
    return (
        *_qmt_order_time_sort_key(order.get("time")),
        numeric_order_id,
        int(order_id) if numeric_order_id else 0,
        order_id,
        sort_sequence,
    )


def _qmt_sorted_orders(orders: Any) -> list[dict[str, Any]] | None:
    if not isinstance(orders, list) or any(not isinstance(order, dict) for order in orders):
        return None
    return sorted(orders, key=_qmt_order_sort_key, reverse=True)


def _qmt_sorted_positions(positions: Any) -> list[dict[str, Any]] | None:
    if not isinstance(positions, list) or any(
        not isinstance(position, dict) for position in positions
    ):
        return None
    return sorted(positions, key=lambda position: str(position.get("code") or "").strip())


def _qmt_protocol_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        text = value.strip()
        if text and text.lstrip("+").isdigit():
            return int(text)
    return None


class QmtBackendClient(QObject):
    """One newline-delimited JSON connection to a QMT socket backend.

    The backend owns polling and pushes the order/position streams after a
    ``sync_request``.  This class keeps the protocol state separate from the
    ETF monitoring WebSocket, so either QMT connection can fail or reconnect
    without disturbing the monitoring client.
    """

    state_changed = pyqtSignal()

    def __init__(self, name: str, host: str, port: int, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.host = host
        self.port = port
        self.socket = QTcpSocket(self)
        self.want_connection = False
        self.buffer = b""
        self.available_cash: float | None = None
        self.orders_by_id: dict[str, dict[str, Any]] = {}
        self.positions: list[dict[str, Any]] = []
        self.last_result: dict[str, Any] = {}
        self.last_error = ""
        self.connection_state = "disconnected"
        self._welcome_received = False
        self._orders_synced = False
        self._positions_synced = False
        self._orders_snapshot_id = ""
        self._orders_seq: int | None = None
        self._positions_snapshot_id = ""
        self._positions_seq: int | None = None
        self._last_backend_message_at = 0.0
        self._last_etf_order_at = 0.0

        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(5_000)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self._try_reconnect)
        self.heartbeat_timer = QTimer(self)
        self.heartbeat_timer.setInterval(QMT_HEARTBEAT_INTERVAL_MS)
        self.heartbeat_timer.timeout.connect(self._heartbeat_tick)
        self.throttle_timer = QTimer(self)
        self.throttle_timer.setSingleShot(True)
        self.throttle_timer.timeout.connect(self.state_changed.emit)

        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.readyRead.connect(self._on_ready_read)
        self.socket.errorOccurred.connect(self._on_error)

    def is_connected(self) -> bool:
        return self.socket.state() == QAbstractSocket.SocketState.ConnectedState

    def is_ready(self) -> bool:
        return self.is_connected() and self.connection_state == "ready"

    def status_text(self) -> str:
        if self.is_ready():
            return "已连接"
        if self.connection_state == "syncing":
            return "同步中…"
        if self.connection_state == "reconnecting":
            return "重连中…"
        if self.connection_state == "connecting" or self.socket.state() in {
                QAbstractSocket.SocketState.HostLookupState,
                QAbstractSocket.SocketState.ConnectingState,
            }:
            return "正在连接…"
        return "未连接"

    def throttle_remaining(self) -> float:
        if self._last_etf_order_at <= 0:
            return 0.0
        return max(
            0.0,
            QMT_ORDER_THROTTLE_SECONDS - (time.monotonic() - self._last_etf_order_at),
        )

    def connect_backend(self) -> None:
        self.want_connection = True
        self.last_error = ""
        if self.is_connected():
            if self.is_ready():
                self.request_sync()
            self.state_changed.emit()
            return
        if self.socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self.socket.abort()
        self.connection_state = "connecting"
        self.socket.connectToHost(self.host, self.port)
        self.state_changed.emit()

    def disconnect_backend(self) -> None:
        self.want_connection = False
        self.reconnect_timer.stop()
        self.heartbeat_timer.stop()
        self.connection_state = "disconnected"
        self._reset_sync_state()
        self.buffer = b""
        self.socket.disconnectFromHost()
        if self.socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self.socket.close()
        self.state_changed.emit()

    def close(self) -> None:
        self.disconnect_backend()

    def request_sync(self) -> None:
        self._send_json({"type": "query_status"})
        self._send_json({"type": "sync_request", "target": "all"})

    def send_etf_order(self, symbol: str, action: str) -> bool:
        if not self.is_ready():
            self.last_error = f"{self.name} 尚未完成连接和数据同步"
            self.state_changed.emit()
            return False
        remaining = self.throttle_remaining()
        if remaining > 0:
            self.last_error = f"{self.name} 操作过快，请 {remaining:.1f} 秒后再试"
            self.state_changed.emit()
            return False
        try:
            code = qmt_etf_code(symbol)
        except ValueError as exc:
            self.last_error = str(exc)
            self.state_changed.emit()
            return False
        normalized_action = str(action).strip().upper()
        if normalized_action not in {"PURCHASE", "REDEEM"}:
            self.last_error = "ETF 操作必须是申购或赎回"
            self.state_changed.emit()
            return False
        action_code = "P" if normalized_action == "PURCHASE" else "R"
        client_order_id = (
            f"ETF-{self.name}-{action_code}-{code.split('.', 1)[0]}-"
            f"{int(time.time() * 1000)}"
        )
        sent = self._send_json(
            {
                "type": "etf_order",
                "action": normalized_action,
                "code": code,
                "qty": 1,
                "client_order_id": client_order_id,
            }
        )
        if sent:
            self._last_etf_order_at = time.monotonic()
            self.throttle_timer.start(int(QMT_ORDER_THROTTLE_SECONDS * 1000) + 25)
            self.last_error = ""
            self.last_result = {
                "pending": True,
                "action": normalized_action,
                "code": code,
                "qty": 1,
                "client_order_id": client_order_id,
                "message": "指令已发送，等待 QMT 后端确认",
            }
        self.state_changed.emit()
        return sent

    def orders_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        target = qmt_etf_code(symbol).split(".", 1)[0]
        return [
            order
            for order in self.orders_by_id.values()
            if str(order.get("code") or "").split(".", 1)[0] == target
        ]

    def _on_connected(self) -> None:
        self.reconnect_timer.stop()
        self.buffer = b""
        self._reset_sync_state()
        self.connection_state = "syncing"
        self._last_backend_message_at = time.monotonic()
        self.heartbeat_timer.start()
        self.last_error = ""
        self._send_ping()
        self.request_sync()
        self.state_changed.emit()

    def _on_disconnected(self) -> None:
        self.heartbeat_timer.stop()
        self.buffer = b""
        self._last_backend_message_at = 0.0
        self._reset_sync_state()
        if self.want_connection:
            self.connection_state = "reconnecting"
            self.reconnect_timer.start()
        else:
            self.connection_state = "disconnected"
        self.state_changed.emit()

    def _on_error(self, _error: Any) -> None:
        self.last_error = self.socket.errorString()
        if self.want_connection:
            self.connection_state = "reconnecting"
            if self.socket.state() == QAbstractSocket.SocketState.UnconnectedState:
                self.reconnect_timer.start()
        self.state_changed.emit()

    def _try_reconnect(self) -> None:
        if self.want_connection and not self.is_connected():
            self.connect_backend()

    def _send_ping(self) -> None:
        self._send_json({"type": "ping"})

    def _heartbeat_tick(self) -> None:
        if not self.is_connected():
            return
        silence = time.monotonic() - self._last_backend_message_at
        if self._last_backend_message_at > 0 and silence > QMT_BACKEND_SILENCE_SECONDS:
            self.last_error = f"{self.name} 后端 {silence:.0f} 秒无响应，正在重连"
            self.connection_state = "reconnecting"
            self.heartbeat_timer.stop()
            self.buffer = b""
            self.socket.abort()
            if self.want_connection and self.socket.state() == QAbstractSocket.SocketState.UnconnectedState:
                self.reconnect_timer.start()
            self.state_changed.emit()
            return
        self._send_ping()

    def _send_json(self, message: dict[str, Any]) -> bool:
        if not self.is_connected():
            return False
        wire = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        if self.socket.write(wire) < 0:
            self.last_error = self.socket.errorString()
            return False
        return True

    def _on_ready_read(self) -> None:
        self.buffer += bytes(self.socket.readAll())
        while b"\n" in self.buffer:
            raw_line, self.buffer = self.buffer.split(b"\n", 1)
            if not raw_line.strip():
                continue
            try:
                message = json.loads(raw_line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.last_error = "QMT 后端返回了无法解析的 JSON 数据"
                self.state_changed.emit()
                continue
            if isinstance(message, dict):
                self._handle_message(message)

    def _reset_sync_state(self) -> None:
        self._welcome_received = False
        self._orders_synced = False
        self._positions_synced = False
        self._orders_snapshot_id = ""
        self._orders_seq = None
        self._positions_snapshot_id = ""
        self._positions_seq = None

    def _maybe_mark_ready(self) -> None:
        if (
            self.is_connected()
            and self._welcome_received
            and self._orders_synced
            and self._positions_synced
        ):
            self.connection_state = "ready"
            self.last_error = ""

    def _reject_sync(self, stream: str, reason: str) -> None:
        self.last_error = reason
        if stream == "orders":
            self._orders_synced = False
        else:
            self._positions_synced = False
        if self.is_connected():
            self.connection_state = "syncing"
            self._send_json({"type": "sync_request", "target": stream})

    def _validate_meta(
        self,
        message: dict[str, Any],
        data: list[dict[str, Any]],
        checksum_data: list[dict[str, Any]],
        *,
        extra: dict[str, Any] | None = None,
    ) -> tuple[str, int] | None:
        snapshot_id = str(message.get("snapshot_id") or "").strip()
        sequence = _qmt_protocol_int(message.get("seq"))
        count = _qmt_protocol_int(message.get("count"))
        expected_checksum = str(message.get("checksum") or "").strip().lower()
        if not snapshot_id or sequence is None or sequence < 0:
            return None
        if count is None or count != len(data):
            return None
        if not expected_checksum or expected_checksum != _qmt_snapshot_checksum(checksum_data, extra):
            return None
        return snapshot_id, sequence

    def _handle_positions(self, message: dict[str, Any]) -> None:
        if message.get("sync_mode") == "error":
            self._reject_sync(
                "positions", str(message.get("error") or "QMT 持仓同步失败")
            )
            return
        if message.get("sync_mode") != "full":
            self._reject_sync("positions", "QMT 持仓同步模式无效，正在请求全量同步")
            return
        data = _qmt_sorted_positions(message.get("data"))
        cash = message.get("available_cash")
        if (
            data is None
            or not isinstance(cash, (int, float))
            or isinstance(cash, bool)
        ):
            self._reject_sync("positions", "QMT 持仓数据格式无效，正在请求全量同步")
            return
        meta = self._validate_meta(
            message,
            data,
            data,
            extra={"available_cash": cash},
        )
        if meta is None:
            self._reject_sync("positions", "QMT 持仓全量校验失败，正在重新同步")
            return
        self.positions = data
        self.available_cash = float(cash)
        self._positions_snapshot_id, self._positions_seq = meta
        self._positions_synced = True
        self._maybe_mark_ready()

    def _handle_orders(self, message: dict[str, Any]) -> None:
        mode = str(message.get("sync_mode") or "").lower()
        if mode == "error":
            self._reject_sync("orders", str(message.get("error") or "QMT 委托同步失败"))
            return
        if mode == "full":
            data = _qmt_sorted_orders(message.get("data"))
            if data is None:
                self._reject_sync("orders", "QMT 委托数据格式无效，正在请求全量同步")
                return
            next_orders: dict[str, dict[str, Any]] = {}
            for order in data:
                order_id = str(order.get("order_id") or "").strip()
                if not order_id or order_id in next_orders:
                    self._reject_sync("orders", "QMT 委托号缺失或重复，正在请求全量同步")
                    return
                next_orders[order_id] = order
            meta = self._validate_meta(message, data, data)
            if meta is None:
                self._reject_sync("orders", "QMT 委托全量校验失败，正在重新同步")
                return
            self.orders_by_id = next_orders
            self._orders_snapshot_id, self._orders_seq = meta
            self._orders_synced = True
            self._maybe_mark_ready()
            return
        if mode != "delta":
            self._reject_sync("orders", "QMT 委托同步模式无效，正在请求全量同步")
            return
        snapshot_id = str(message.get("snapshot_id") or "").strip()
        sequence = _qmt_protocol_int(message.get("seq"))
        if (
            not self._orders_synced
            or not self._orders_snapshot_id
            or snapshot_id != self._orders_snapshot_id
            or self._orders_seq is None
            or sequence != self._orders_seq + 1
        ):
            self._reject_sync("orders", "QMT 委托增量缺少基础快照或序号不连续，正在重新同步")
            return
        upserts = message.get("upserts")
        remove_ids = message.get("remove_ids")
        if not isinstance(upserts, list) or not isinstance(remove_ids, list):
            self._reject_sync("orders", "QMT 委托增量格式无效，正在重新同步")
            return
        next_orders = dict(self.orders_by_id)
        for order in upserts:
            if not isinstance(order, dict):
                self._reject_sync("orders", "QMT 委托增量格式无效，正在重新同步")
                return
            order_id = str(order.get("order_id") or "").strip()
            if not order_id:
                self._reject_sync("orders", "QMT 委托增量缺少委托号，正在重新同步")
                return
            next_orders[order_id] = order
        for order_id in remove_ids:
            next_orders.pop(str(order_id), None)
        checksum_data = sorted(next_orders.values(), key=_qmt_order_sort_key, reverse=True)
        meta = self._validate_meta(message, checksum_data, checksum_data)
        if meta is None:
            self._reject_sync("orders", "QMT 委托增量数量或校验和不一致，正在重新同步")
            return
        self.orders_by_id = next_orders
        self._orders_snapshot_id, self._orders_seq = meta
        self._orders_synced = True
        self._maybe_mark_ready()

    def _handle_message(self, message: dict[str, Any]) -> None:
        self._last_backend_message_at = time.monotonic()
        message_type = str(message.get("type") or "")
        if message_type == "welcome":
            if message.get("push_sync") is True:
                self._welcome_received = True
                self._maybe_mark_ready()
            else:
                self.last_error = "QMT 后端不支持所需的推送同步协议"
                if self.is_connected():
                    self.connection_state = "syncing"
        elif message_type == "positions_data":
            self._handle_positions(message)
        elif message_type == "orders_data":
            self._handle_orders(message)
        elif message_type == "etf_order_result":
            self.last_result = message
            if not message.get("success"):
                self.last_error = str(message.get("error") or "ETF 申赎指令被后端拒绝")
            else:
                self.last_error = ""
                self.request_sync()
        elif message_type == "error":
            self.last_error = str(message.get("detail") or message.get("error") or "QMT 后端错误")
        self.state_changed.emit()


def resource_path(*parts: str) -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return root.joinpath(*parts)


def setting_bool(settings: QSettings, key: str, default: bool) -> bool:
    return str(settings.value(key, "true" if default else "false")).lower() == "true"


def split_address(value: str, default_port: int = 6787) -> tuple[str, int]:
    clean = value.strip()
    for prefix in ("http://", "https://", "ws://", "wss://"):
        if clean.startswith(prefix):
            clean = clean[len(prefix) :]
    clean = clean.rstrip("/")
    if clean.startswith("[") and "]:" in clean:
        host, raw_port = clean[1:].split("]:", 1)
    elif clean.count(":") == 1:
        host, raw_port = clean.rsplit(":", 1)
    else:
        host, raw_port = clean, str(default_port)
    try:
        port = int(raw_port)
    except ValueError:
        port = default_port
    return host or "127.0.0.1", max(1, min(port, 65535))


def format_share_value(value: Any, signed: bool = False) -> str:
    """Format shares in four-digit Chinese units (for example ``100 0000``)."""

    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    rounded = int(round(value))
    digits = str(abs(rounded))
    groups: list[str] = []
    while digits:
        groups.append(digits[-4:])
        digits = digits[:-4]
    text = " ".join(reversed(groups)) or "0"
    if rounded < 0:
        return f"-{text}"
    if signed and rounded > 0:
        return f"+{text}"
    return text


def basket_count(item: dict[str, Any], amount_field: str) -> float | None:
    """Return an amount's basket count using the current PCF minimum unit."""

    values = item.get("values") or {}
    pcf = item.get("pcf") or {}
    amount = values.get(amount_field)
    unit = pcf.get("creation_redemption_unit")
    if pcf.get("status") != "ready":
        return None
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    if not isinstance(unit, (int, float)) or isinstance(unit, bool) or unit <= 0:
        return None
    return float(amount) / float(unit)


def format_basket_count(value: float | None, signed: bool = False) -> str:
    if value is None:
        return "待确认"
    rounded = round(value)
    if abs(value - rounded) < 1e-9:
        return format_share_value(rounded, signed=signed)
    text = f"{abs(value):.2f}".rstrip("0").rstrip(".")
    if value < 0:
        return f"-{text}"
    if signed and value > 0:
        return f"+{text}"
    return text


def classify_local_net_creation_quota(item: dict[str, Any]) -> dict[str, Any]:
    """Classify creation capacity from cumulative and net PCF limits locally.

    A zero limit means that specific constraint is not set.  A positive
    ``CreationLimit`` is compared with cumulative creation shares, while a
    positive ``NetCreationLimit`` is compared with the creation/redemption net.
    """

    result: dict[str, Any] = {
        "kind": "pending",
        "label": "待确认",
        "creation_shares": None,
        "net_shares": None,
        "creation_limit_shares": None,
        "net_limit_shares": None,
        "limit_shares": None,
        "remaining_shares": None,
        "reason": "等待实时份额和 PCF",
    }
    values = item.get("values") or {}
    pcf = item.get("pcf") or {}
    creation_shares = values.get("etfbuyamount")
    net_shares = values.get("netamount")
    if not all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (creation_shares, net_shares)
    ):
        result["reason"] = "实时申购份额或轧差份额尚未就绪"
        return result
    result["creation_shares"] = creation_shares
    result["net_shares"] = net_shares
    if pcf.get("status") != "ready":
        result["reason"] = "当日 PCF 尚未就绪"
        return result

    creation_limit = pcf.get("creation_limit")
    net_limit = pcf.get("net_creation_limit")
    creation_limit_valid = isinstance(creation_limit, (int, float)) and not isinstance(
        creation_limit, bool
    )
    net_limit_valid = isinstance(net_limit, (int, float)) and not isinstance(
        net_limit, bool
    )
    result["creation_limit_shares"] = creation_limit if creation_limit_valid else None
    result["net_limit_shares"] = net_limit if net_limit_valid else None

    if creation_limit_valid and creation_limit > 0 and creation_shares >= creation_limit:
        result.update(
            kind="full",
            label="已满",
            limit_shares=creation_limit,
            remaining_shares=0,
            reason=(
                f"当前累计申购 {format_share_value(creation_shares)}，"
                f"已达到 PCF 累计上限 {format_share_value(creation_limit)}"
            ),
        )
        return result
    if net_limit_valid and net_limit > 0 and net_shares >= net_limit:
        result.update(
            kind="full",
            label="已满",
            limit_shares=net_limit,
            remaining_shares=0,
            reason=(
                f"当前净申购 {format_share_value(net_shares)}，"
                f"已达到 PCF 净申购上限 {format_share_value(net_limit)}"
            ),
        )
        return result
    if pcf.get("creation_allowed") is False:
        result.update(
            kind="closed",
            label="申购关闭",
            reason="PCF 明确关闭申购",
        )
        return result
    if not creation_limit_valid or not net_limit_valid:
        result["reason"] = "PCF 的累计或净申购上限字段不完整"
        return result

    constraints: list[tuple[str, float]] = []
    if creation_limit > 0:
        constraints.append(("累计", max(0.0, float(creation_limit) - float(creation_shares))))
    if net_limit > 0:
        constraints.append(("净申购", max(0.0, float(net_limit) - float(net_shares))))
    if not constraints:
        result.update(
            kind="available",
            label="未满",
            reason="PCF 未设置正数累计申购或净申购上限",
        )
        return result

    binding_name, remaining = min(constraints, key=lambda constraint: constraint[1])
    result.update(
        kind="available",
        label="未满",
        remaining_shares=remaining,
        reason=(
            f"累计申购 {format_share_value(creation_shares)}，"
            f"净申购 {format_share_value(net_shares)}；"
            f"距离 PCF {binding_name}限额还剩 {format_share_value(remaining)}"
        ),
    )
    return result


class ClientAlertPopup(QDialog):
    def __init__(self, message: str) -> None:
        super().__init__(None)
        self.setWindowTitle("数据变化提醒")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.resize(540, 230)
        layout = QVBoxLayout(self)
        heading = QLabel("检测到申购赎回份额变化")
        heading.setStyleSheet("font-size: 18px; font-weight: 700; color: #172033;")
        content = QLabel(message)
        content.setWordWrap(True)
        close_button = QPushButton("知道了")
        close_button.clicked.connect(self.close)
        layout.addWidget(heading)
        layout.addWidget(content, 1)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        self.setStyleSheet(
            "QDialog { background: white; } QLabel { color: #253044; }"
            "QPushButton { color: white; background: #2f6feb; border: 0; "
            "border-radius: 6px; padding: 8px 18px; }"
        )


class ChangeBanner(QFrame):
    """A persistent, dismissible summary of the latest pushed change."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("changeBanner")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(14, 10, 10, 10)
        layout.setSpacing(10)
        self.message_label = QLabel()
        self.message_label.setWordWrap(True)
        self.message_label.setObjectName("changeBannerText")
        self.close_button = QToolButton()
        self.close_button.setText("×")
        self.close_button.setToolTip("关闭本次变化横幅")
        self.close_button.setObjectName("changeBannerClose")
        layout.addWidget(self.message_label, 1)
        layout.addWidget(self.close_button, 0, Qt.AlignmentFlag.AlignTop)
        self.hide()

    def set_message(self, message: str) -> None:
        self.message_label.setText(message)


class PcfDetailDialog(QDialog):
    def __init__(
        self,
        payload: dict[str, Any],
        parent: QWidget | None = None,
        *,
        name_save_callback: Any | None = None,
        qmt_clients: dict[str, QmtBackendClient] | None = None,
    ) -> None:
        super().__init__(parent)
        symbol = str(payload.get("symbol") or "")
        self.symbol = symbol
        self.name_save_callback = name_save_callback
        self.qmt_clients = qmt_clients or {}
        self.qmt_tabs: dict[str, dict[str, Any]] = {}
        self.setObjectName("pcfDetailDialog")
        self.setWindowTitle(f"{symbol} PCF 详细信息")
        self.resize(1120, 720)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 24, 28, 22)
        layout.setSpacing(16)
        self.setStyleSheet(
            """
            QDialog#pcfDetailDialog { background: #f5f7fa; }
            QDialog#pcfDetailDialog QLabel { color: #253044; background: transparent; }
            QTabWidget#pcfDetailTabs::pane {
                background: white; border: 1px solid #dce3ec; border-radius: 10px;
                top: -1px;
            }
            QTabWidget#pcfDetailTabs QTabBar::tab {
                color: #526074; background: #edf2f7; border: 1px solid #dce3ec;
                border-bottom: 0; border-top-left-radius: 8px; border-top-right-radius: 8px;
                min-width: 132px; min-height: 28px; margin-right: 7px; padding: 8px 18px;
                font-size: 15px; font-weight: 700;
            }
            QTabWidget#pcfDetailTabs QTabBar::tab:hover { background: #e0e9f5; color: #2f6feb; }
            QTabWidget#pcfDetailTabs QTabBar::tab:selected {
                color: white; background: #2f6feb; border-color: #2f6feb;
            }
            QWidget#qmtTradePage { background: transparent; }
            QFrame#qmtTradeCard {
                background: white; border: 1px solid #dce3ec; border-radius: 10px;
            }
            QLabel#qmtConnectionStatus { font-size: 17px; font-weight: 700; }
            QLabel#qmtCash { color: #526f8e; font-size: 16px; font-weight: 600; }
            QLabel#qmtResult { font-size: 15px; }
            QPushButton#qmtPurchaseButton {
                color: white; background: #168553; border: 1px solid #168553;
                border-radius: 10px; padding: 14px 30px; font-size: 19px; font-weight: 700;
            }
            QPushButton#qmtPurchaseButton:hover { background: #0f6c42; }
            QPushButton#qmtRedeemButton {
                color: white; background: #c53b45; border: 1px solid #c53b45;
                border-radius: 10px; padding: 14px 30px; font-size: 19px; font-weight: 700;
            }
            QPushButton#qmtRedeemButton:hover { background: #aa2733; }
            QPushButton#pcfCloseButton {
                color: #253044; background: white; border: 1px solid #cbd5e1;
                border-radius: 7px; padding: 9px 20px; font-weight: 600;
            }
            QPushButton#pcfCloseButton:hover { background: #eef3f9; }
            QTableWidget {
                color: #172033; background: white; border: 1px solid #dce3ec;
                border-radius: 8px; gridline-color: #e2e8f0; font-size: 14px;
            }
            QTableWidget::item { padding: 7px; }
            QHeaderView::section {
                background: #eef2f7; color: #526074; border: none;
                border-right: 1px solid #dce3ec; border-bottom: 1px solid #dce3ec;
                padding: 9px; font-size: 14px; font-weight: 700;
            }
            """
        )

        heading = QLabel(
            f"{symbol}  {payload.get('name') or payload.get('fund_name') or ''}  ·  "
            f"PCF {payload.get('trading_day') or '—'}"
        )
        heading.setStyleSheet("font-size: 18px; font-weight: 700; color: #172033;")
        layout.addWidget(heading)

        if self.name_save_callback is not None:
            name_row = QHBoxLayout()
            name_row.addWidget(QLabel("自定义名称"))
            self.name_input = QLineEdit(str(payload.get("custom_name") or ""))
            self.name_input.setPlaceholderText("例如：日经套利；留空恢复 PCF 名称")
            self.name_input.setMaxLength(40)
            self.name_save_button = QPushButton("保存名称")
            self.name_save_button.clicked.connect(self._save_name)
            name_row.addWidget(self.name_input, 1)
            name_row.addWidget(self.name_save_button)
            layout.addLayout(name_row)

        tabs = QTabWidget()
        tabs.setObjectName("pcfDetailTabs")
        tabs.tabBar().setExpanding(False)
        summary = payload.get("summary_fields") or []
        summary_table = QTableWidget(len(summary), 2)
        summary_table.setHorizontalHeaderLabels(["字段", "值"])
        summary_table.verticalHeader().setVisible(False)
        summary_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        summary_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        summary_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        for row, field in enumerate(summary):
            summary_table.setItem(row, 0, QTableWidgetItem(str(field.get("label") or field.get("field") or "")))
            summary_table.setItem(row, 1, QTableWidgetItem(str(field.get("value") or "")))
        tabs.addTab(summary_table, "清单摘要")

        columns = payload.get("component_columns") or []
        components = payload.get("components") or []
        component_table = QTableWidget(len(components), len(columns))
        component_table.setHorizontalHeaderLabels(
            [str(column.get("label") or column.get("field") or "") for column in columns]
        )
        component_table.verticalHeader().setVisible(False)
        component_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        component_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        for row, component in enumerate(components):
            for column, descriptor in enumerate(columns):
                field = str(descriptor.get("field") or "")
                component_table.setItem(row, column, QTableWidgetItem(str(component.get(field) or "")))
        tabs.addTab(component_table, f"成分证券（{len(components)}）")

        for backend_name, client in self.qmt_clients.items():
            tabs.addTab(self._build_qmt_tab(backend_name, client), backend_name)
            client.state_changed.connect(self._refresh_qmt_tabs)
        layout.addWidget(tabs, 1)

        if payload.get("error"):
            error = QLabel(f"提示：{payload['error']}")
            error.setWordWrap(True)
            error.setStyleSheet("color: #c53b45;")
            layout.addWidget(error)
        close_button = QPushButton("关闭")
        close_button.setObjectName("pcfCloseButton")
        close_button.clicked.connect(self.close)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)

        self._refresh_qmt_tabs()

    def _build_qmt_tab(self, backend_name: str, client: QmtBackendClient) -> QWidget:
        page = QWidget()
        page.setObjectName("qmtTradePage")
        page_layout = QVBoxLayout(page)
        page_layout.setContentsMargins(12, 12, 12, 12)
        card = QFrame()
        card.setObjectName("qmtTradeCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)

        endpoint = f"{client.host}:{client.port}"
        status = QLabel()
        status.setObjectName("qmtConnectionStatus")
        status.setWordWrap(True)
        cash = QLabel()
        cash.setObjectName("qmtCash")
        result = QLabel()
        result.setWordWrap(True)
        result.setObjectName("qmtResult")
        layout.addWidget(status)
        layout.addWidget(cash)

        action_row = QHBoxLayout()
        action_row.setSpacing(28)
        purchase_button = DoubleClickButton("双击申购 1 篮子")
        purchase_button.setObjectName("qmtPurchaseButton")
        purchase_button.setMinimumSize(272, 68)
        purchase_button.setToolTip(f"双击后向 {backend_name} 提交 {self.symbol} 的 1 个申购篮子")
        purchase_button.doubleClicked.connect(
            lambda name=backend_name: self._submit_qmt_order(name, "PURCHASE")
        )
        redeem_button = DoubleClickButton("双击赎回 1 篮子")
        redeem_button.setObjectName("qmtRedeemButton")
        redeem_button.setMinimumSize(272, 68)
        redeem_button.setToolTip(f"双击后向 {backend_name} 提交 {self.symbol} 的 1 个赎回篮子")
        redeem_button.doubleClicked.connect(
            lambda name=backend_name: self._submit_qmt_order(name, "REDEEM")
        )
        action_row.addWidget(purchase_button)
        action_row.addWidget(redeem_button)
        action_row.addStretch()
        layout.addLayout(action_row)
        layout.addWidget(result)

        orders_title = QLabel("当日委托（当前标的）")
        orders_title.setStyleSheet("font-weight: 700; color: #253044;")
        orders_table = QTableWidget(0, 6)
        orders_table.setHorizontalHeaderLabels(["时间", "方向", "状态", "委托量", "成交量", "委托号"])
        orders_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        orders_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        orders_table.verticalHeader().setVisible(False)
        orders_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        orders_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(orders_title)
        layout.addWidget(orders_table, 1)
        page_layout.addWidget(card)

        self.qmt_tabs[backend_name] = {
            "endpoint": endpoint,
            "status": status,
            "cash": cash,
            "result": result,
            "orders": orders_table,
            "purchase": purchase_button,
            "redeem": redeem_button,
        }
        return page

    def _submit_qmt_order(self, backend_name: str, action: str) -> None:
        client = self.qmt_clients[backend_name]
        action_text = "申购" if action == "PURCHASE" else "赎回"
        if not client.is_ready():
            QMessageBox.warning(
                self,
                f"{backend_name} 尚未就绪",
                f"请等待 {backend_name} 完成连接及委托、持仓同步，再双击{action_text}。",
            )
            return
        if client.send_etf_order(self.symbol, action):
            self._refresh_qmt_tabs()
        else:
            QMessageBox.warning(self, "指令未发送", client.last_error or "QMT 连接不可用")

    def _refresh_qmt_tabs(self) -> None:
        for backend_name, widgets in self.qmt_tabs.items():
            client = self.qmt_clients[backend_name]
            ready = client.is_ready()
            state_text = client.status_text()
            transitional = any(word in state_text for word in ("正在", "同步", "重连"))
            state_color = "#168553" if ready else "#d97706" if transitional else "#c53b45"
            widgets["status"].setText(
                f"{backend_name} · {state_text} · {widgets['endpoint']}"
            )
            widgets["status"].setStyleSheet(f"font-weight: 700; color: {state_color};")
            widgets["cash"].setText(
                "可用资金："
                + (f"¥{client.available_cash:,.2f}" if client.available_cash is not None else "等待后端同步")
            )
            can_submit = ready and client.throttle_remaining() <= 0
            widgets["purchase"].setEnabled(can_submit)
            widgets["redeem"].setEnabled(can_submit)

            result = client.last_result
            result_code = str(result.get("code") or "").split(".", 1)[0]
            own_result = result if result_code == self.symbol.split(".", 1)[0] else {}
            if own_result:
                if own_result.get("pending"):
                    result_text = str(own_result.get("message") or "指令已发送")
                    result_color = "#d97706"
                elif own_result.get("success"):
                    result_text = str(own_result.get("message") or "ETF 指令已提交")
                    result_color = "#168553"
                else:
                    result_text = str(own_result.get("error") or "ETF 指令失败")
                    result_color = "#c53b45"
                widgets["result"].setText(f"最近指令：{result_text}")
                widgets["result"].setStyleSheet(f"font-weight: 600; color: {result_color};")
            elif client.last_error:
                widgets["result"].setText(f"后端提示：{client.last_error}")
                widgets["result"].setStyleSheet("font-weight: 600; color: #c53b45;")
            else:
                widgets["result"].setText("最近指令：—")
                widgets["result"].setStyleSheet("color: #68758a;")

            orders = client.orders_for_symbol(self.symbol)
            orders.sort(key=lambda order: str(order.get("time") or ""), reverse=True)
            table: QTableWidget = widgets["orders"]
            table.setRowCount(len(orders))
            for row, order in enumerate(orders):
                values = [
                    str(order.get("time") or "—"),
                    str(order.get("direction") or "—"),
                    str(order.get("status") or "—"),
                    str(order.get("qty") if order.get("qty") is not None else "—"),
                    str(order.get("traded_qty") if order.get("traded_qty") is not None else "—"),
                    str(order.get("order_id") or "—"),
                ]
                for column, value in enumerate(values):
                    cell = QTableWidgetItem(value)
                    cell.setTextAlignment(
                        Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
                    )
                    table.setItem(row, column, cell)

    def _save_name(self) -> None:
        if self.name_save_callback is None:
            return
        self.name_save_callback(self.symbol, self.name_input.text())
        self.name_save_button.setText("已提交")
        self.name_save_button.setEnabled(False)


class ClientSettingsDialog(QDialog):
    def __init__(
        self,
        values: dict[str, Any],
        preview_callback: Any,
        *,
        server_editable: bool = True,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.preview_callback = preview_callback
        self.setWindowTitle("设置")
        self.resize(560, 560)
        root = QVBoxLayout(self)

        connection_group = QGroupBox("服务器连接")
        connection_form = QFormLayout(connection_group)
        self.host_input = QLineEdit(str(values["server_host"]))
        self.host_input.setPlaceholderText("例如 192.168.1.20")
        self.port_input = QSpinBox()
        self.port_input.setRange(1, 65535)
        self.port_input.setValue(int(values["server_port"]))
        self.auto_connect_check = QCheckBox("程序启动后自动连接")
        self.auto_connect_check.setChecked(bool(values["auto_connect"]))
        self.reconnect_input = QSpinBox()
        self.reconnect_input.setRange(1, 120)
        self.reconnect_input.setSuffix(" 秒")
        self.reconnect_input.setValue(int(values["reconnect_seconds"]))
        self.heartbeat_input = QSpinBox()
        self.heartbeat_input.setRange(5, 120)
        self.heartbeat_input.setSuffix(" 秒")
        self.heartbeat_input.setValue(int(values["heartbeat_seconds"]))
        connection_form.addRow("服务器 IP / 主机名", self.host_input)
        connection_form.addRow("端口", self.port_input)
        connection_form.addRow("", self.auto_connect_check)
        connection_form.addRow("断线重连间隔", self.reconnect_input)
        connection_form.addRow("连接心跳间隔", self.heartbeat_input)
        if not server_editable:
            self.host_input.setEnabled(False)
            self.port_input.setEnabled(False)
            self.auto_connect_check.setEnabled(False)
            self.host_input.setToolTip("Mac 主机程序固定连接本机服务；远程客户端可修改此项。")
        root.addWidget(connection_group)

        alert_group = QGroupBox("变化提醒")
        alert_form = QFormLayout(alert_group)
        self.popup_check = QCheckBox("显示置顶弹窗")
        self.popup_check.setChecked(bool(values["popup"]))
        self.sound_check = QCheckBox("播放声音")
        self.sound_check.setChecked(bool(values["sound"]))
        switches = QHBoxLayout()
        switches.addWidget(self.popup_check)
        switches.addWidget(self.sound_check)
        switches.addStretch()
        alert_form.addRow("提醒方式", switches)

        self.sound_combo = QComboBox()
        for sound_id, label, _filename in PRESET_SOUNDS:
            self.sound_combo.addItem(label, sound_id)
        selected = self.sound_combo.findData(str(values["sound_id"]))
        self.sound_combo.setCurrentIndex(max(0, selected))
        alert_form.addRow("提示音", self.sound_combo)

        self.sound_repeat_input = QSpinBox()
        self.sound_repeat_input.setRange(1, 10)
        self.sound_repeat_input.setSuffix(" 次")
        self.sound_repeat_input.setValue(int(values["sound_repeat_count"]))
        self.sound_repeat_input.setToolTip(
            "每次变化提醒完整播放该段音频的次数；清亮三音默认 3 次，共 9 响。"
        )
        alert_form.addRow("重复播放", self.sound_repeat_input)

        external_row = QHBoxLayout()
        self.external_input = QLineEdit(str(values["external_sound_path"]))
        self.external_input.setPlaceholderText("选择 WAV、MP3、M4A、FLAC 等音频")
        browse = QPushButton("选择…")
        browse.clicked.connect(self._browse_sound)
        external_row.addWidget(self.external_input, 1)
        external_row.addWidget(browse)
        alert_form.addRow("外部音频", external_row)

        volume_row = QHBoxLayout()
        self.volume_slider = QSlider(Qt.Orientation.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setValue(int(values["volume"]))
        self.volume_label = QLabel(f"{self.volume_slider.value()}%")
        self.volume_slider.valueChanged.connect(
            lambda value: self.volume_label.setText(f"{value}%")
        )
        preview = QPushButton("立即试听")
        preview.clicked.connect(self._preview)
        volume_row.addWidget(self.volume_slider, 1)
        volume_row.addWidget(self.volume_label)
        volume_row.addWidget(preview)
        alert_form.addRow("音量", volume_row)

        self.popup_duration_input = QSpinBox()
        self.popup_duration_input.setRange(0, 120)
        self.popup_duration_input.setSuffix(" 秒")
        self.popup_duration_input.setSpecialValueText("不自动关闭")
        self.popup_duration_input.setValue(int(values["popup_duration_seconds"]))
        self.cooldown_input = QSpinBox()
        self.cooldown_input.setRange(0, 300)
        self.cooldown_input.setSuffix(" 秒")
        self.cooldown_input.setSpecialValueText("不限制")
        self.cooldown_input.setValue(int(values["alert_cooldown_seconds"]))
        alert_form.addRow("弹窗自动关闭", self.popup_duration_input)
        alert_form.addRow("提醒冷却时间", self.cooldown_input)
        root.addWidget(alert_group)

        note = QLabel("冷却时间只抑制连续声音和弹窗，不影响表格更新与数据接收。")
        note.setWordWrap(True)
        note.setObjectName("detail")
        root.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    def _browse_sound(self) -> None:
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择提醒音频",
            self.external_input.text().strip(),
            "音频文件 (*.wav *.mp3 *.m4a *.aac *.ogg *.flac);;所有文件 (*)",
        )
        if path:
            self.external_input.setText(path)
            index = self.sound_combo.findData("external")
            self.sound_combo.setCurrentIndex(index)

    def _preview(self) -> None:
        self.preview_callback(
            str(self.sound_combo.currentData()),
            self.external_input.text().strip(),
            self.volume_slider.value(),
            self.sound_repeat_input.value(),
            True,
        )

    def values(self) -> dict[str, Any]:
        return {
            "server_host": self.host_input.text().strip() or "127.0.0.1",
            "server_port": self.port_input.value(),
            "auto_connect": self.auto_connect_check.isChecked(),
            "reconnect_seconds": self.reconnect_input.value(),
            "heartbeat_seconds": self.heartbeat_input.value(),
            "popup": self.popup_check.isChecked(),
            "sound": self.sound_check.isChecked(),
            "sound_id": str(self.sound_combo.currentData()),
            "external_sound_path": self.external_input.text().strip(),
            "sound_repeat_count": self.sound_repeat_input.value(),
            "volume": self.volume_slider.value(),
            "popup_duration_seconds": self.popup_duration_input.value(),
            "alert_cooldown_seconds": self.cooldown_input.value(),
        }


class RemoteClientWindow(QMainWindow):
    def __init__(
        self,
        *,
        settings_name: str = "ETFRemoteClient",
        window_title: str = "ETF 远程监控",
        default_address: str = "127.0.0.1:6787",
        server_editable: bool = True,
        server_controls: bool = False,
    ) -> None:
        super().__init__()
        self.settings = QSettings("ETFDelivery", settings_name)
        self.default_address = default_address
        self.server_editable = server_editable
        self.server_controls = server_controls
        self.socket = QWebSocket()
        self.network = QNetworkAccessManager(self)
        self.qmt_clients = {
            name: QmtBackendClient(name, host, port, self)
            for name, (host, port) in QMT_BACKENDS.items()
        }
        self.qmt_connect_buttons: dict[str, QPushButton] = {}
        self.want_connection = False
        self.items: dict[str, dict[str, Any]] = {}
        self.alert_popups: list[ClientAlertPopup] = []
        self.pcf_dialogs: list[PcfDetailDialog] = []
        self.changed_symbols: set[str] = set()
        # Symbols whose server-side opportunity/change history has been accepted as
        # the local baseline.  Keep this separate from ``items`` because regular
        # snapshot refreshes still contain the server's last change.
        self.baseline_suppressed_symbols: set[str] = set()
        self.baseline_established = False
        self.last_alert_at = 0.0
        self.audio_output = QAudioOutput(self)
        self.audio_player = QMediaPlayer(self)
        self.audio_player.setAudioOutput(self.audio_output)

        self.setWindowTitle(window_title)
        self.resize(1280, 680)
        self._build_ui()
        self._apply_style()
        self._restore_settings()
        for client in self.qmt_clients.values():
            client.state_changed.connect(self._refresh_qmt_connection_buttons)

        self.socket.connected.connect(self._on_connected)
        self.socket.disconnected.connect(self._on_disconnected)
        self.socket.textMessageReceived.connect(self._on_text_message)
        self.socket.errorOccurred.connect(self._on_socket_error)
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setInterval(self.reconnect_seconds * 1000)
        self.reconnect_timer.timeout.connect(self._try_reconnect)
        self.ping_timer = QTimer(self)
        self.ping_timer.setInterval(self.heartbeat_seconds * 1000)
        self.ping_timer.timeout.connect(self._send_ping)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)
        self.root_layout = layout

        connection = QFrame()
        connection.setObjectName("card")
        self.connection_card = connection
        grid = QGridLayout(connection)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(10)
        # The endpoint is configured in Settings.  Keep a hidden state widget
        # for backwards-compatible connection helpers, but do not spend home
        # screen space on a duplicate editable address.
        self.address_input = QLineEdit()
        self.address_input.setPlaceholderText("例如 192.168.1.20:6787")
        self.address_input.returnPressed.connect(self.connect_server)
        self.address_input.hide()

        self.connect_button = QPushButton()
        self.connect_button.setObjectName("connectionButton")
        self.connect_button.setMinimumWidth(156)
        self.connect_button.clicked.connect(self.toggle_server_connection)
        self._set_connection_button_state("disconnected")
        for backend_name in self.qmt_clients:
            qmt_button = QPushButton()
            qmt_button.setMinimumWidth(132)
            qmt_button.clicked.connect(
                lambda _checked=False, name=backend_name: self.toggle_qmt_connection(name)
            )
            self.qmt_connect_buttons[backend_name] = qmt_button
        # Kept as an attribute for Mac-host compatibility; remote clients use
        # the single coloured connection button above.
        self.disconnect_button = QPushButton("断开")
        self.disconnect_button.hide()
        self.pull_button = QPushButton("拉取当前全量")
        self.pull_button.clicked.connect(self.pull_snapshot)
        self.pcf_refresh_button = QPushButton("刷新 PCF")
        self.pcf_refresh_button.clicked.connect(
            lambda: self._post("/api/v1/pcf/refresh")
        )
        self.remote_start_button = QPushButton("开始监控")
        self.remote_start_button.clicked.connect(
            lambda: self._post("/api/v1/monitor/start")
        )
        self.remote_stop_button = QPushButton("停止监控")
        self.remote_stop_button.clicked.connect(
            lambda: self._post("/api/v1/monitor/stop")
        )
        self.settings_button = QPushButton("设置…")
        self.settings_button.clicked.connect(self.open_settings)
        self.reset_baseline_button = QPushButton("重置变化基准")
        self.reset_baseline_button.clicked.connect(self.reset_change_baseline)
        self.reset_baseline_button.setEnabled(False)
        self.popup_check = QCheckBox("弹窗提醒")
        self.sound_check = QCheckBox("声音提醒")
        self.popup_check.setChecked(True)
        self.sound_check.setChecked(True)
        buttons = QHBoxLayout()
        toolbar_widgets = [self.connect_button, *self.qmt_connect_buttons.values(), self.pull_button]
        if self.server_controls:
            toolbar_widgets.extend(
                [
                    self.pcf_refresh_button,
                    self.remote_start_button,
                    self.remote_stop_button,
                ]
            )
        toolbar_widgets.extend(
            [
                self.settings_button,
                self.reset_baseline_button,
                self.popup_check,
                self.sound_check,
            ]
        )
        for widget in toolbar_widgets:
            buttons.addWidget(widget)
        buttons.addStretch()
        grid.addLayout(buttons, 0, 0)
        layout.addWidget(connection)

        watchlist = QFrame()
        watchlist.setObjectName("card")
        self.watchlist_card = watchlist
        watch_layout = QHBoxLayout(watchlist)
        watch_layout.setContentsMargins(16, 10, 16, 10)
        self.watchlist_label = QLabel("观察列表")
        watch_layout.addWidget(self.watchlist_label)
        self.symbol_input = QLineEdit()
        self.symbol_input.setPlaceholderText("6 位深圳代码")
        self.symbol_input.setMaximumWidth(180)
        self.add_symbol_button = QPushButton("添加")
        self.add_symbol_button.clicked.connect(self.add_remote_symbol)
        self.remove_symbol_button = QPushButton("删除选中")
        self.remove_symbol_button.clicked.connect(self.remove_remote_selected)
        if self.server_controls:
            watch_layout.addWidget(self.symbol_input)
            watch_layout.addWidget(self.add_symbol_button)
            watch_layout.addWidget(self.remove_symbol_button)
        watch_layout.addStretch()
        self.connection_label = QLabel("● 未连接")
        watch_layout.addWidget(self.connection_label)
        if self.server_controls:
            layout.addWidget(watchlist)
        else:
            watchlist.hide()

        self.table = QTableWidget(0, 13)
        self.table.setObjectName("monitorTable")
        self.table.setHorizontalHeaderLabels(
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
                "申购额度",
                "机会判断",
                "更新时间",
                "最近变化",
            ]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.cellDoubleClicked.connect(self._open_pcf_for_row)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(12, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table, 1)

        self.footer_label = QLabel("WebSocket 等待连接")
        self.footer_label.setObjectName("detail")
        layout.addWidget(self.footer_label)
        self.setCentralWidget(root)

        self.change_banner = ChangeBanner(root)
        self.change_banner.close_button.clicked.connect(self.hide_change_banner)
        self.change_banner_timer = QTimer(self)
        self.change_banner_timer.setSingleShot(True)
        self.change_banner_timer.timeout.connect(self.hide_change_banner)

        settings_menu = self.menuBar().addMenu("设置")
        preferences_action = settings_menu.addAction("偏好设置…")
        preferences_action.triggered.connect(self.open_settings)
        preview_action = settings_menu.addAction("试听当前提示音")
        preview_action.triggered.connect(lambda: self.play_alert_sound(preview=True))
        self._refresh_qmt_connection_buttons()

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#appRoot { background: #f5f7fa; color: #1f2937; }
            QLabel, QCheckBox { background: transparent; color: #253044; }
            QCheckBox::indicator { width: 15px; height: 15px; background: white;
                                    border: 1px solid #aab7c7; border-radius: 3px; }
            QCheckBox::indicator:checked { background: #2f6feb; border-color: #2f6feb; }
            QFrame#card { background: white; border: 1px solid #dce3ec;
                          border-radius: 10px; }
            QLineEdit, QComboBox, QSpinBox { color: #172033; background: white; border: 1px solid #cbd5e1;
                        border-radius: 6px; padding: 7px; min-height: 20px; }
            QGroupBox { color: #253044; border: 1px solid #dce3ec; border-radius: 8px;
                        margin-top: 10px; padding-top: 10px; font-weight: 600; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 5px; }
            QPushButton { color: #283548; background: #f8fafc; border: 1px solid #cbd5e1;
                          border-radius: 6px; padding: 8px 14px; }
            QPushButton:hover { background: #eef3f9; }
            QPushButton:disabled { color: #9aa5b4; background: #f1f4f8; }
            QPushButton#primaryButton { color: white; background: #2f6feb;
                                        border-color: #2f6feb; }
            QPushButton#qmtPurchaseButton { color: white; background: #168553;
                                            border-color: #168553; font-weight: 700; }
            QPushButton#qmtPurchaseButton:hover { background: #0f6c42; }
            QPushButton#qmtRedeemButton { color: white; background: #c53b45;
                                          border-color: #c53b45; font-weight: 700; }
            QPushButton#qmtRedeemButton:hover { background: #aa2733; }
            QFrame#changeBanner { background: #fff7df; border: 1px solid #f1c453;
                                  border-radius: 9px; }
            QLabel#changeBannerText { color: #6a4600; font-weight: 600; }
            QToolButton#changeBannerClose { color: #6a4600; font-size: 21px; border: 0;
                                            padding: 0 4px; }
            QTableWidget#monitorTable { color: #172033; background: white;
                          border: 1px solid #dce3ec; border-radius: 10px;
                          gridline-color: #e2e8f0; font-size: 13px; }
            QTableWidget#monitorTable::item { padding: 6px; }
            QTableWidget#monitorTable::item:selected { background: #dce9ff; color: #172033; }
            QHeaderView::section { background: #eef2f7; color: #526074; border: none;
                          border-right: 1px solid #dce3ec; border-bottom: 1px solid #dce3ec;
                          padding: 8px; font-weight: 600; }
            QLabel#detail { color: #68758a; }
            """
        )

    def _restore_settings(self) -> None:
        old_address = str(self.settings.value("address", self.default_address))
        default_host, default_port = split_address(old_address)
        self.server_host = str(self.settings.value("server_host", default_host))
        self.server_port = int(self.settings.value("server_port", default_port))
        # v2 deliberately defaults to manual connection.  The legacy key is
        # ignored so existing installations also become manual after upgrade.
        self.auto_connect_enabled = setting_bool(self.settings, "auto_connect_v2", False)
        self.reconnect_seconds = int(self.settings.value("reconnect_seconds", 5))
        self.heartbeat_seconds = int(self.settings.value("heartbeat_seconds", 15))
        self.popup_enabled = setting_bool(self.settings, "popup", True)
        self.sound_enabled = setting_bool(self.settings, "sound", True)
        self.sound_id = str(self.settings.value("sound_id", "bright"))
        self.external_sound_path = str(self.settings.value("external_sound_path", ""))
        self.sound_repeat_count = max(
            1, min(10, int(self.settings.value("sound_repeat_count", 3)))
        )
        self.alert_volume = int(self.settings.value("volume", 75))
        self.popup_duration_seconds = int(
            self.settings.value("popup_duration_seconds", 12)
        )
        self.alert_cooldown_seconds = int(
            self.settings.value("alert_cooldown_seconds", 0)
        )
        self.address_input.setText(f"{self.server_host}:{self.server_port}")
        self.popup_check.setChecked(self.popup_enabled)
        self.sound_check.setChecked(self.sound_enabled)
        self.audio_output.setVolume(self.alert_volume / 100.0)

    def _save_settings(self) -> None:
        host, port = split_address(self.address_input.text(), self.server_port)
        self.server_host, self.server_port = host, port
        self.popup_enabled = self.popup_check.isChecked()
        self.sound_enabled = self.sound_check.isChecked()
        values = self._settings_values()
        self.settings.setValue("address", f"{host}:{port}")
        for key, value in values.items():
            self.settings.setValue(key, value)
        self.settings.sync()

    def _settings_values(self) -> dict[str, Any]:
        return {
            "server_host": self.server_host,
            "server_port": self.server_port,
            "auto_connect": self.auto_connect_enabled,
            "auto_connect_v2": self.auto_connect_enabled,
            "reconnect_seconds": self.reconnect_seconds,
            "heartbeat_seconds": self.heartbeat_seconds,
            "popup": self.popup_enabled,
            "sound": self.sound_enabled,
            "sound_id": self.sound_id,
            "external_sound_path": self.external_sound_path,
            "sound_repeat_count": self.sound_repeat_count,
            "volume": self.alert_volume,
            "popup_duration_seconds": self.popup_duration_seconds,
            "alert_cooldown_seconds": self.alert_cooldown_seconds,
        }

    def open_settings(self) -> None:
        old_endpoint = self._host_port()
        dialog = ClientSettingsDialog(
            self._settings_values(),
            self._play_sound,
            server_editable=self.server_editable,
            parent=self,
        )
        dialog.setStyleSheet(self.styleSheet())
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        self.server_host = values["server_host"]
        self.server_port = values["server_port"]
        self.auto_connect_enabled = values["auto_connect"]
        self.reconnect_seconds = values["reconnect_seconds"]
        self.heartbeat_seconds = values["heartbeat_seconds"]
        self.popup_enabled = values["popup"]
        self.sound_enabled = values["sound"]
        self.sound_id = values["sound_id"]
        self.external_sound_path = values["external_sound_path"]
        self.sound_repeat_count = values["sound_repeat_count"]
        self.alert_volume = values["volume"]
        self.popup_duration_seconds = values["popup_duration_seconds"]
        self.alert_cooldown_seconds = values["alert_cooldown_seconds"]
        self.address_input.setText(f"{self.server_host}:{self.server_port}")
        self.popup_check.setChecked(self.popup_enabled)
        self.sound_check.setChecked(self.sound_enabled)
        self.reconnect_timer.setInterval(self.reconnect_seconds * 1000)
        self.ping_timer.setInterval(self.heartbeat_seconds * 1000)
        self.audio_output.setVolume(self.alert_volume / 100.0)
        self._save_settings()
        self.footer_label.setText("设置已保存")
        if self.want_connection and old_endpoint != self._host_port():
            self.connect_server()

    def _sound_path(self, sound_id: str, external_path: str) -> Path | None:
        if sound_id == "external":
            return Path(external_path).expanduser() if external_path else None
        filename = next(
            (filename for key, _label, filename in PRESET_SOUNDS if key == sound_id),
            "bright.wav",
        )
        return resource_path("assets", "sounds", filename)

    def _play_sound(
        self,
        sound_id: str,
        external_path: str,
        volume: int,
        repeat_count: int,
        preview: bool = False,
    ) -> bool:
        path = self._sound_path(sound_id, external_path)
        if path is None or not path.is_file():
            if preview:
                QMessageBox.warning(self, "无法播放", "请选择存在的外部音频文件。")
            return False
        self.audio_output.setVolume(max(0, min(volume, 100)) / 100.0)
        self.audio_player.stop()
        self.audio_player.setLoops(max(1, min(int(repeat_count), 10)))
        self.audio_player.setSource(QUrl.fromLocalFile(str(path.resolve())))
        self.audio_player.play()
        return True

    def play_alert_sound(self, *, preview: bool = False) -> bool:
        return self._play_sound(
            self.sound_id,
            self.external_sound_path,
            self.alert_volume,
            self.sound_repeat_count,
            preview,
        )

    def _host_port(self) -> str:
        value = self.address_input.text().strip()
        for prefix in ("http://", "https://", "ws://", "wss://"):
            if value.startswith(prefix):
                value = value[len(prefix) :]
        return value.rstrip("/")

    def _set_connection_button_state(self, state: str) -> None:
        labels = {
            "disconnected": ("未连接 · 点击连接", "#c53b45", "#aa2733"),
            "connecting": ("正在连接…", "#d97706", "#b45309"),
            "connected": ("已连接 · 点击断开", "#168553", "#0f6c42"),
        }
        text, background, border = labels[state]
        self.connect_button.setText(text)
        self.connect_button.setStyleSheet(
            "QPushButton { color: white; background: %s; border: 1px solid %s; "
            "border-radius: 6px; padding: 8px 14px; font-weight: 600; } "
            "QPushButton:hover { background: %s; }" % (background, border, border)
        )
        self.connect_button.setProperty("connection_state", state)

    def _refresh_qmt_connection_buttons(self) -> None:
        for backend_name, button in self.qmt_connect_buttons.items():
            client = self.qmt_clients[backend_name]
            state_text = client.status_text()
            if client.is_ready():
                background, border = "#168553", "#0f6c42"
            elif any(word in state_text for word in ("正在", "同步", "重连")):
                background, border = "#d97706", "#b45309"
            else:
                background, border = "#c53b45", "#aa2733"
            button.setText(f"{backend_name} · {state_text}")
            button.setToolTip(f"{client.host}:{client.port} · 点击连接或断开")
            button.setStyleSheet(
                "QPushButton { color: white; background: %s; border: 1px solid %s; "
                "border-radius: 6px; padding: 8px 12px; font-weight: 600; } "
                "QPushButton:hover { background: %s; }" % (background, border, border)
            )

    def toggle_qmt_connection(self, backend_name: str) -> None:
        client = self.qmt_clients[backend_name]
        if client.want_connection or client.is_connected():
            client.disconnect_backend()
            self.footer_label.setText(f"{backend_name} 已断开")
            return
        client.connect_backend()
        self.footer_label.setText(f"正在连接 {backend_name} · {client.host}:{client.port}")

    def toggle_server_connection(self) -> None:
        if self.want_connection or self.socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self.disconnect_server()
            return
        self.connect_server()

    def connect_server(self) -> None:
        host = self._host_port()
        if not host:
            QMessageBox.warning(self, "连接信息不完整", "请输入主机地址。")
            return
        self._save_settings()
        self.want_connection = True
        self.connection_label.setText("● 正在连接…")
        self.connection_label.setStyleSheet("color: #d97706;")
        self._set_connection_button_state("connecting")
        if self.socket.state() != QAbstractSocket.SocketState.UnconnectedState:
            self.socket.abort()
        url = QUrl(f"ws://{host}/ws/v1/changes")
        self.socket.open(url)

    def disconnect_server(self) -> None:
        self.want_connection = False
        self.reconnect_timer.stop()
        self.ping_timer.stop()
        self.socket.close()
        self.connection_label.setText("● 未连接")
        self.connection_label.setStyleSheet("color: #c53b45;")
        self._set_connection_button_state("disconnected")

    def _try_reconnect(self) -> None:
        if (
            self.want_connection
            and self.socket.state() == QAbstractSocket.SocketState.UnconnectedState
        ):
            self.connect_server()

    def _on_connected(self) -> None:
        self.connection_label.setText("● 已连接")
        self.connection_label.setStyleSheet("color: #168553;")
        self._set_connection_button_state("connected")
        self.reconnect_timer.stop()
        self.ping_timer.start()
        self.footer_label.setText("WebSocket 已连接，等待主机推送")
        self.pull_snapshot()

    def _on_disconnected(self) -> None:
        self.connection_label.setText("● 已断开")
        self.connection_label.setStyleSheet("color: #c53b45;")
        self._set_connection_button_state("disconnected")
        self.ping_timer.stop()
        if self.want_connection:
            self.reconnect_timer.start()

    def _send_ping(self) -> None:
        if self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            self.socket.sendTextMessage(json.dumps({"type": "ping"}))

    def _on_socket_error(self, _error: Any) -> None:
        self.footer_label.setText(f"连接错误：{self.socket.errorString()}")

    def _on_text_message(self, text: str) -> None:
        try:
            event = json.loads(text)
        except json.JSONDecodeError:
            return
        event_type = event.get("type")
        if event_type == "snapshot":
            self._apply_snapshot(event)
        elif event_type == "change":
            self._apply_change(event)
        elif event_type == "status":
            self.footer_label.setText(str(event.get("message", "")))
        elif event_type == "heartbeat":
            self.footer_label.setText(f"心跳 {event.get('server_time', '')}")
        elif event_type == "pong":
            self.footer_label.setText(f"连接正常 · {event.get('server_time', '')}")

    def _apply_snapshot(self, event: dict[str, Any]) -> None:
        self.items = {
            str(item.get("symbol")): item for item in event.get("items", [])
        }
        # A full snapshot must not resurrect changes that this client has already
        # accepted as its baseline.  Drop only symbols no longer in the watchlist.
        self.baseline_suppressed_symbols.intersection_update(self.items)
        self._rebuild_table()
        if not self.baseline_established:
            self.baseline_established = True
            self.reset_baseline_button.setEnabled(bool(self.items))
        state = "监控中" if event.get("monitoring") else "未监控"
        error = str(event.get("last_error") or "")
        self.footer_label.setText(
            error
            or f"全量已更新 · {len(self.items)} 个标的 · 主机{state} · {event.get('server_time', '')}"
        )

    def _apply_change(self, event: dict[str, Any]) -> None:
        messages: list[str] = []
        for changed in event.get("items", []):
            symbol = str(changed.get("symbol", ""))
            current = changed.get("current")
            if isinstance(current, dict):
                self.items[symbol] = current
            # A real change push is the only event that releases the local reset
            # suppression for this symbol.
            self.baseline_suppressed_symbols.discard(symbol)
            if self.baseline_established and symbol:
                self.changed_symbols.add(symbol)
            details = [str(item.get("text", "")) for item in changed.get("changes", [])]
            if isinstance(current, dict):
                opportunity = current.get("opportunity") or {}
                if opportunity.get("label"):
                    details.append(f"机会判断 {opportunity['label']}")
            name = str(current.get("name") or "").strip() if isinstance(current, dict) else ""
            identity = f"{symbol}  {name}" if name else symbol
            messages.append(f"{identity}：{'；'.join(details)}")
        self._rebuild_table()
        if not messages:
            return
        combined = "\n".join(messages)
        self.footer_label.setText(f"收到变化推送 · {event.get('server_time', '')}")
        self.show_change_banner(combined, event.get("server_time", ""))
        now = time.monotonic()
        if self.alert_cooldown_seconds and now - self.last_alert_at < self.alert_cooldown_seconds:
            self.footer_label.setText(
                f"收到变化推送 · 提醒冷却中 · {event.get('server_time', '')}"
            )
            return
        alerted = False
        if self.sound_check.isChecked():
            if not self.play_alert_sound():
                QApplication.beep()
            alerted = True
        if self.popup_check.isChecked():
            popup = ClientAlertPopup(combined)
            self.alert_popups.append(popup)
            popup.finished.connect(lambda _result, item=popup: self._remove_popup(item))
            popup.show()
            if self.popup_duration_seconds > 0:
                QTimer.singleShot(self.popup_duration_seconds * 1000, popup.close)
            alerted = True
        if alerted:
            self.last_alert_at = now

    def reset_change_baseline(self) -> None:
        self.changed_symbols.clear()
        self.baseline_suppressed_symbols = set(self.items)
        self.baseline_established = bool(self.items)
        self._rebuild_table()
        self.hide_change_banner()
        if self.items:
            self.footer_label.setText(
                f"变化基准已重置 · {len(self.items)} 个标的以当前状态为准"
            )
        else:
            self.footer_label.setText("尚未收到数据；连接后可重置变化基准")
        self.reset_baseline_button.setEnabled(bool(self.items))

    def show_change_banner(self, message: str, server_time: Any = "") -> None:
        timestamp = str(server_time or datetime.now().strftime("%H:%M:%S"))
        summary = message.replace("\n", "  ·  ")
        self.change_banner.set_message(f"发生变化 · {timestamp}\n{summary}")
        self._position_change_banner()
        self.change_banner.show()
        self.change_banner.raise_()
        self.change_banner_timer.start(60_000)

    def hide_change_banner(self) -> None:
        self.change_banner_timer.stop()
        self.change_banner.hide()

    def _position_change_banner(self) -> None:
        root = self.centralWidget()
        if root is None:
            return
        width = min(640, max(360, root.width() - 44))
        height = 82
        self.change_banner.resize(width, height)
        self.change_banner.move(max(12, root.width() - width - 22), max(12, root.height() - height - 22))

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        if hasattr(self, "change_banner"):
            self._position_change_banner()

    def _remove_popup(self, popup: ClientAlertPopup) -> None:
        if popup in self.alert_popups:
            self.alert_popups.remove(popup)

    @staticmethod
    def _format(value: Any, signed: bool = False) -> str:
        return format_share_value(value, signed=signed)

    @staticmethod
    def _status_text(value: Any) -> str:
        return {
            "waiting": "等待数据",
            "connecting": "连接中",
            "monitoring": "监控中",
            "cached": "缓存数据",
            "stopped": "已停止",
            "error": "错误",
            "reconnecting": "重连中",
        }.get(str(value), str(value or "—"))

    @staticmethod
    def _format_time(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return "—"
        if "T" in text:
            text = text.split("T", 1)[1]
        if "+" in text:
            text = text.split("+", 1)[0]
        if text.endswith("Z"):
            text = text[:-1]
        return text[:8] if len(text) >= 8 else text

    def _rebuild_table(self) -> None:
        symbols = sorted(self.items)
        self.table.setRowCount(len(symbols))
        for row, symbol in enumerate(symbols):
            item = self.items[symbol]
            values = item.get("values", {})
            baseline_suppressed = symbol in self.baseline_suppressed_symbols
            changes = [] if baseline_suppressed else item.get("last_change", [])
            opportunity = {} if baseline_suppressed else item.get("opportunity") or {}
            net_creation_quota = classify_local_net_creation_quota(item)
            row_values = [
                symbol,
                item.get("name") or "—",
                self._status_text(item.get("status")),
                self._format(values.get("etfbuyamount")),
                self._format(values.get("etfsellamount")),
                self._format(values.get("netamount"), signed=True),
                format_basket_count(basket_count(item, "etfbuyamount")),
                format_basket_count(basket_count(item, "etfsellamount")),
                format_basket_count(basket_count(item, "netamount"), signed=True),
                net_creation_quota["label"],
                "等待盘中变化"
                if baseline_suppressed
                else opportunity.get("label") or "待确认",
                self._format_time(item.get("updated_at")),
                "；".join(str(change.get("text", "")) for change in changes) or "—",
            ]
            for column, value in enumerate(row_values):
                cell = QTableWidgetItem(str(value))
                cell.setTextAlignment(
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
                )
                if column in {5, 8} and isinstance(values.get("netamount"), (int, float)):
                    net = values["netamount"]
                    cell.setForeground(QColor("#168553" if net > 0 else "#c53b45" if net < 0 else "#526074"))
                if column == 9:
                    quota_kind = str(net_creation_quota.get("kind") or "")
                    cell.setForeground(
                        QColor(
                            "#168553"
                            if quota_kind == "available"
                            else "#c53b45"
                            if quota_kind in {"full", "closed"}
                            else "#68758a"
                        )
                    )
                    cell.setToolTip(str(net_creation_quota.get("reason") or ""))
                if column == 10:
                    kind = str(opportunity.get("kind") or "")
                    cell.setForeground(
                        QColor("#168553" if kind == "creation" else "#c53b45" if kind == "redemption" else "#68758a")
                    )
                if symbol in self.changed_symbols:
                    cell.setBackground(QColor("#fff0bd"))
                self.table.setItem(row, column, cell)

    def _request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        request_kind: str = "",
    ) -> None:
        host = self._host_port()
        if not host:
            return
        request = QNetworkRequest(QUrl(f"http://{host}{path}"))
        request.setHeader(QNetworkRequest.KnownHeaders.ContentTypeHeader, "application/json")
        payload = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")
        if method == "GET":
            reply = self.network.get(request)
        elif method == "PUT":
            reply = self.network.put(request, payload)
        else:
            reply = self.network.post(request, payload)
        reply.setProperty("request_kind", request_kind)
        reply.finished.connect(lambda item=reply: self._http_finished(item))

    def _http_finished(self, reply: QNetworkReply) -> None:
        request_kind = str(reply.property("request_kind") or "")
        data = bytes(reply.readAll())
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        reply.deleteLater()
        try:
            payload = json.loads(data) if data else {}
        except json.JSONDecodeError:
            payload = {}
        if status is None or int(status) >= 400:
            self.footer_label.setText(
                f"请求失败 HTTP {status}: {payload.get('detail', data.decode(errors='replace'))}"
            )
            return
        if request_kind == "pcf-detail":
            self._show_pcf_detail(payload)
            return
        if payload.get("type") == "snapshot":
            self._apply_snapshot(payload)
        elif "symbols" in payload:
            self.pull_snapshot()

    def pull_snapshot(self) -> None:
        if self.socket.state() == QAbstractSocket.SocketState.ConnectedState:
            self.socket.sendTextMessage(json.dumps({"type": "get_snapshot"}))
        else:
            self._request("GET", "/api/v1/snapshot")

    def _post(self, path: str) -> None:
        self._request("POST", path)

    def _open_pcf_for_row(self, row: int, _column: int) -> None:
        symbol_cell = self.table.item(row, 0)
        if symbol_cell is None:
            return
        symbol = symbol_cell.text().strip()
        self.footer_label.setText(f"正在读取 {symbol} 的当日 PCF…")
        self._request(
            "GET", f"/api/v1/pcf/{symbol}", request_kind="pcf-detail"
        )

    def _show_pcf_detail(self, payload: dict[str, Any]) -> None:
        dialog = PcfDetailDialog(
            payload,
            self,
            name_save_callback=self._save_symbol_name if self.server_controls else None,
            qmt_clients=self.qmt_clients,
        )
        self.pcf_dialogs.append(dialog)
        dialog.finished.connect(
            lambda _result, item=dialog: self._remove_pcf_dialog(item)
        )
        dialog.show()
        dialog.raise_()
        self.footer_label.setText(
            f"PCF 已打开 · {payload.get('symbol', '')} · {payload.get('trading_day') or '无日期'}"
        )

    def _remove_pcf_dialog(self, dialog: PcfDetailDialog) -> None:
        if dialog in self.pcf_dialogs:
            self.pcf_dialogs.remove(dialog)

    def _save_symbol_name(self, symbol: str, name: str) -> None:
        self.footer_label.setText(f"正在保存 {symbol} 的名称…")
        self._request(
            "PUT",
            f"/api/v1/symbols/{symbol}/name",
            {"name": name},
            request_kind="symbol-name",
        )

    def add_remote_symbol(self) -> None:
        code = self.symbol_input.text().strip()
        if len(code) != 6 or not code.isdigit():
            QMessageBox.warning(self, "代码错误", "请输入 6 位深圳代码。")
            return
        symbols = sorted(set(self.items) | {code})
        self._request("PUT", "/api/v1/watchlist", {"symbols": symbols})
        self.symbol_input.clear()

    def remove_remote_selected(self) -> None:
        selected = {index.row() for index in self.table.selectedIndexes()}
        remove = {
            self.table.item(row, 0).text() for row in selected if self.table.item(row, 0)
        }
        symbols = [symbol for symbol in sorted(self.items) if symbol not in remove]
        if not symbols:
            QMessageBox.warning(self, "不能删除", "观察列表至少保留一个标的。")
            return
        self._request("PUT", "/api/v1/watchlist", {"symbols": symbols})

    def closeEvent(self, event: Any) -> None:
        self.want_connection = False
        self._save_settings()
        self.ping_timer.stop()
        self.socket.close()
        for client in self.qmt_clients.values():
            client.close()
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ETF 远程监控")
    app.setOrganizationName("ETFDelivery")
    font = QFont()
    font.setPointSize(13)
    app.setFont(font)
    window = RemoteClientWindow(default_address="192.168.1.113:6787")
    window.show()
    if window.auto_connect_enabled:
        QTimer.singleShot(0, window.connect_server)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
