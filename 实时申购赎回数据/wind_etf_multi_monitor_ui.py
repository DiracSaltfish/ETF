#!/usr/bin/env python3
"""Multi-symbol Shenzhen ETF change monitor."""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QRegularExpression, QSettings, Qt, QTimer
from PyQt6.QtGui import QBrush, QColor, QFont, QRegularExpressionValidator
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from wind_etf_realtime_ui import (
    APP_LOG_PATH,
    OperationThread,
    ProbeController,
    ProbeError,
    SubscriptionSession,
    WIND_TEMP_DIR,
    display_symbol,
    normalize_symbol,
    safe_code,
)
from wind_tbapi_frame_parser import FrameFormatError, decode_probe_capture


ALERT_METRICS: list[tuple[str, str, bool, bool]] = [
    ("etfbuyamount", "申购份额", True, False),
    ("etfsellamount", "赎回份额", True, False),
]


class AlertPopup(QDialog):
    def __init__(self, message: str) -> None:
        super().__init__(None)
        self.setWindowTitle("数据变化提醒")
        self.setWindowFlags(
            Qt.WindowType.Dialog
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.resize(520, 220)
        layout = QVBoxLayout(self)
        heading = QLabel("检测到申购赎回数据变化")
        heading.setStyleSheet("font-size: 18px; font-weight: 700; color: #172033;")
        content = QLabel(message)
        content.setWordWrap(True)
        content.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        close_button = QPushButton("知道了")
        close_button.clicked.connect(self.close)
        layout.addWidget(heading)
        layout.addWidget(content, 1)
        layout.addWidget(close_button, 0, Qt.AlignmentFlag.AlignRight)
        self.setStyleSheet(
            "QDialog { background: #ffffff; }"
            "QLabel { color: #253044; }"
            "QPushButton { color: #ffffff; background: #2f6feb; border: 0; "
            "border-radius: 6px; padding: 8px 18px; }"
        )


class MonitorWindow(QMainWindow):
    def __init__(
        self,
        capture_dir: Path = WIND_TEMP_DIR,
        log_path: Path = APP_LOG_PATH,
    ) -> None:
        super().__init__()
        self.capture_dir = capture_dir
        self.log_path = log_path
        self.controller = ProbeController()
        self.settings = QSettings("ETFDelivery", "ETFMultiMonitor")
        self.sessions: dict[str, SubscriptionSession] = {}
        self.worker: OperationThread | None = None
        self.symbols: list[str] = []
        self.last_values: dict[str, dict[str, Any]] = {}
        self.last_alert_messages: list[str] = []
        self.log_lines: list[str] = []
        self.log_dialog: QDialog | None = None
        self.log_view: QTextEdit | None = None
        self.alert_popups: list[AlertPopup] = []

        self.setWindowTitle("ETF 变化监控")
        self.resize(1240, 680)
        self._build_ui()
        self._apply_style()
        self._restore_settings()

        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_data)
        self._update_timer()
        QTimer.singleShot(0, self.refresh_data)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("appRoot")
        layout = QVBoxLayout(root)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(14)

        controls = QFrame()
        controls.setObjectName("card")
        grid = QGridLayout(controls)
        grid.setContentsMargins(16, 14, 16, 14)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)

        grid.addWidget(QLabel("深圳代码"), 0, 0)
        self.symbol_input = QLineEdit()
        self.symbol_input.setPlaceholderText("例如 159518")
        self.symbol_input.setValidator(
            QRegularExpressionValidator(QRegularExpression(r"[0-9]{0,6}"))
        )
        self.symbol_input.returnPressed.connect(self.add_symbol)
        grid.addWidget(self.symbol_input, 0, 1)

        self.add_button = QPushButton("添加")
        self.add_button.setObjectName("primaryButton")
        self.add_button.clicked.connect(self.add_symbol)
        self.remove_button = QPushButton("删除选中")
        self.remove_button.clicked.connect(self.remove_selected)
        grid.addWidget(self.add_button, 0, 2)
        grid.addWidget(self.remove_button, 0, 3)

        grid.addWidget(QLabel("刷新频率"), 0, 4)
        self.interval_spin = QDoubleSpinBox()
        self.interval_spin.setRange(0.5, 60.0)
        self.interval_spin.setSingleStep(0.5)
        self.interval_spin.setDecimals(1)
        self.interval_spin.setSuffix(" 秒")
        self.interval_spin.valueChanged.connect(self._update_timer)
        grid.addWidget(self.interval_spin, 0, 5)

        self.popup_check = QCheckBox("弹窗提醒")
        self.sound_check = QCheckBox("声音提醒")
        self.popup_check.setChecked(True)
        self.sound_check.setChecked(True)
        grid.addWidget(self.popup_check, 0, 6)
        grid.addWidget(self.sound_check, 0, 7)

        buttons = QHBoxLayout()
        self.start_button = QPushButton("开始全部监控")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_all)
        self.stop_button = QPushButton("停止全部监控")
        self.stop_button.clicked.connect(self.stop_all)
        self.stop_button.setEnabled(False)
        self.refresh_button = QPushButton("立即刷新")
        self.refresh_button.clicked.connect(self.refresh_data)
        self.log_button = QPushButton("查看日志")
        self.log_button.clicked.connect(self.show_log_dialog)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.refresh_button)
        buttons.addWidget(self.log_button)
        buttons.addStretch()
        grid.addLayout(buttons, 1, 0, 1, 8)
        layout.addWidget(controls)

        summary = QFrame()
        summary.setObjectName("card")
        summary_layout = QHBoxLayout(summary)
        summary_layout.setContentsMargins(16, 10, 16, 10)
        self.monitor_label = QLabel("● 尚未开始监控")
        self.monitor_label.setObjectName("monitorStatus")
        self.change_label = QLabel("等待建立数据基线")
        summary_layout.addWidget(self.monitor_label)
        summary_layout.addSpacing(18)
        summary_layout.addWidget(self.change_label)
        summary_layout.addStretch()
        self.count_label = QLabel("0 个标的")
        summary_layout.addWidget(self.count_label)
        layout.addWidget(summary)

        self.table = QTableWidget(0, 9)
        self.table.setObjectName("monitorTable")
        self.table.setHorizontalHeaderLabels(
            [
                "标的",
                "状态",
                "申购笔数",
                "申购份额",
                "赎回笔数",
                "赎回份额",
                "轧差份额",
                "数据时间",
                "最近变化",
            ]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(8, QHeaderView.ResizeMode.Stretch)
        self.table.setMinimumHeight(400)
        layout.addWidget(self.table, 1)

        self.footer_label = QLabel("首次读取只建立基线；后续任一数值变化才会提醒。")
        self.footer_label.setObjectName("detail")
        layout.addWidget(self.footer_label)
        self.setCentralWidget(root)

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget#appRoot { background: #f5f7fa; color: #1f2937; }
            QLabel, QCheckBox { background: transparent; color: #253044; }
            QCheckBox::indicator { width: 15px; height: 15px; background: #ffffff;
                                    border: 1px solid #aab7c7; border-radius: 3px; }
            QCheckBox::indicator:checked { background: #2f6feb;
                                            border-color: #2f6feb; }
            QFrame#card { background: #ffffff; border: 1px solid #dce3ec;
                          border-radius: 10px; }
            QLineEdit, QDoubleSpinBox { color: #172033; background: #ffffff;
                          border: 1px solid #cbd5e1; border-radius: 6px;
                          padding: 7px; min-height: 20px; }
            QLineEdit:focus, QDoubleSpinBox:focus { border: 1px solid #3b72e8; }
            QPushButton { color: #283548; background: #f8fafc;
                          border: 1px solid #cbd5e1; border-radius: 6px;
                          padding: 8px 14px; }
            QPushButton:hover { background: #eef3f9; }
            QPushButton:disabled { color: #9aa5b4; background: #f1f4f8; }
            QPushButton#primaryButton { color: white; background: #2f6feb;
                                        border-color: #2f6feb; }
            QTableWidget#monitorTable { color: #172033; background: #ffffff;
                          border: 1px solid #dce3ec; border-radius: 10px;
                          gridline-color: #e2e8f0; font-size: 13px; }
            QTableWidget#monitorTable::item { background: #ffffff; padding: 6px; }
            QTableWidget#monitorTable::item:selected { background: #dce9ff;
                                                       color: #172033; }
            QHeaderView::section { background: #eef2f7; color: #526074;
                          border: none; border-right: 1px solid #dce3ec;
                          border-bottom: 1px solid #dce3ec; padding: 8px;
                          font-weight: 600; }
            QTextEdit { background: #ffffff; border: 1px solid #dce3ec;
                        border-radius: 8px; padding: 8px; color: #526074;
                        font-family: Menlo, monospace; font-size: 11px; }
            QLabel#detail { color: #68758a; }
            """
        )

    def _restore_settings(self) -> None:
        self.interval_spin.setValue(float(self.settings.value("interval", 1.0)))
        self.popup_check.setChecked(
            str(self.settings.value("popup", "true")).lower() == "true"
        )
        self.sound_check.setChecked(
            str(self.settings.value("sound", "true")).lower() == "true"
        )
        saved = self.settings.value("symbols", [])
        if isinstance(saved, str):
            saved_symbols = [part for part in saved.split(",") if part]
        else:
            saved_symbols = list(saved) if saved else []
        if not saved_symbols:
            legacy = QSettings("ETFDelivery", "WindETFRealtime").value(
                "symbol", "159518"
            )
            saved_symbols = [str(legacy)]
        for code in saved_symbols:
            try:
                symbol = normalize_symbol(str(code))
            except ValueError:
                continue
            if symbol not in self.symbols:
                self.symbols.append(symbol)
        if not self.symbols:
            self.symbols = ["159518.SZ"]
        self._rebuild_table()

    def _save_settings(self) -> None:
        self.settings.setValue(
            "symbols", [display_symbol(symbol) for symbol in self.symbols]
        )
        self.settings.setValue("interval", self.interval_spin.value())
        self.settings.setValue("popup", self.popup_check.isChecked())
        self.settings.setValue("sound", self.sound_check.isChecked())

    def _update_timer(self) -> None:
        if hasattr(self, "refresh_timer"):
            self.refresh_timer.start(int(self.interval_spin.value() * 1000))

    def _rebuild_table(self) -> None:
        self.table.setRowCount(len(self.symbols))
        for row, symbol in enumerate(self.symbols):
            for column in range(self.table.columnCount()):
                item = QTableWidgetItem("—")
                item.setTextAlignment(
                    Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
                )
                self.table.setItem(row, column, item)
            self.table.item(row, 0).setText(display_symbol(symbol))
            self.table.item(row, 1).setText("未监控")
        self.count_label.setText(f"{len(self.symbols)} 个标的")

    def _row_for_symbol(self, symbol: str) -> int:
        return self.symbols.index(symbol)

    def add_symbol(self) -> None:
        if self.sessions:
            QMessageBox.information(self, "请先停止", "修改观察列表前请先停止全部监控。")
            return
        try:
            symbol = normalize_symbol(self.symbol_input.text())
        except ValueError as exc:
            QMessageBox.warning(self, "代码格式错误", str(exc))
            return
        if symbol not in self.symbols:
            self.symbols.append(symbol)
            self._rebuild_table()
            self._save_settings()
            self.refresh_data()
        self.symbol_input.clear()

    def remove_selected(self) -> None:
        if self.sessions:
            QMessageBox.information(self, "请先停止", "修改观察列表前请先停止全部监控。")
            return
        rows = sorted({index.row() for index in self.table.selectedIndexes()}, reverse=True)
        for row in rows:
            symbol = self.symbols.pop(row)
            self.last_values.pop(symbol, None)
        if rows:
            self._rebuild_table()
            self._save_settings()

    def append_log(self, message: str) -> None:
        line = f"[{datetime.now().strftime('%H:%M:%S')}] {message}"
        self.log_lines.append(line)
        self.log_lines = self.log_lines[-1000:]
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError:
            pass
        if self.log_view is not None:
            self.log_view.append(line)

    def show_log_dialog(self) -> None:
        if self.log_dialog is not None:
            self.log_dialog.show()
            self.log_dialog.raise_()
            return
        dialog = QDialog(self)
        dialog.setWindowTitle("运行日志")
        dialog.resize(800, 380)
        layout = QVBoxLayout(dialog)
        view = QTextEdit()
        view.setReadOnly(True)
        view.setPlainText("\n".join(self.log_lines))
        close_button = QPushButton("关闭")
        close_button.clicked.connect(dialog.close)
        layout.addWidget(view)
        layout.addWidget(close_button)
        dialog.finished.connect(self._log_dialog_closed)
        self.log_dialog = dialog
        self.log_view = view
        dialog.show()

    def _log_dialog_closed(self, _result: int) -> None:
        self.log_dialog = None
        self.log_view = None

    def start_all(self) -> None:
        if not self.symbols or (self.worker and self.worker.isRunning()):
            return
        latency_ms = int(self.interval_spin.value() * 1000)
        self._save_settings()
        self._set_busy(True)
        self.monitor_label.setText("● 正在建立批量订阅…")
        for row in range(self.table.rowCount()):
            self.table.item(row, 1).setText("连接中")
        self.append_log(
            f"开始监控 {len(self.symbols)} 个标的，刷新频率 {latency_ms}ms"
        )
        self.worker = OperationThread(
            lambda: self.controller.subscribe_many(self.symbols, latency_ms)
        )
        self.worker.succeeded.connect(self._all_started)
        self.worker.failed.connect(self._operation_failed)
        self.worker.start()

    def _all_started(self, result: object) -> None:
        sessions, errors = result  # type: ignore[misc]
        self.sessions = dict(sessions)
        for symbol in self.symbols:
            row = self._row_for_symbol(symbol)
            if symbol in self.sessions:
                session = self.sessions[symbol]
                self.table.item(row, 1).setText(f"监控中 · {session.sub_id}")
                self.append_log(
                    f"{display_symbol(symbol)} 订阅成功，ID={session.sub_id}"
                )
            else:
                self.table.item(row, 1).setText("订阅失败")
                self.append_log(f"{display_symbol(symbol)} 失败：{errors.get(symbol, '')}")
        self.monitor_label.setText(
            f"● 正在监控 {len(self.sessions)}/{len(self.symbols)} 个标的"
        )
        self.monitor_label.setStyleSheet(
            "color: #168553;" if self.sessions else "color: #c53b45;"
        )
        self._set_busy(False)
        self.start_button.setEnabled(not bool(self.sessions))
        self.stop_button.setEnabled(bool(self.sessions))
        self.add_button.setEnabled(not bool(self.sessions))
        self.remove_button.setEnabled(not bool(self.sessions))
        self.refresh_data()

    def stop_all(self) -> None:
        if not self.sessions or (self.worker and self.worker.isRunning()):
            return
        sessions = dict(self.sessions)
        self._set_busy(True)
        self.monitor_label.setText("● 正在停止全部监控…")
        self.worker = OperationThread(lambda: self.controller.stop_many(sessions))
        self.worker.succeeded.connect(self._all_stopped)
        self.worker.failed.connect(self._operation_failed)
        self.worker.start()

    def _all_stopped(self, result: object) -> None:
        errors = dict(result) if isinstance(result, dict) else {}
        if errors:
            self.append_log(f"停止时出现错误：{errors}")
        self.sessions.clear()
        for row in range(self.table.rowCount()):
            self.table.item(row, 1).setText("已停止")
        self.monitor_label.setText("● 已停止监控")
        self.monitor_label.setStyleSheet("")
        self._set_busy(False)
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)
        self.add_button.setEnabled(True)
        self.remove_button.setEnabled(True)
        self.append_log("全部订阅已停止")

    def _set_busy(self, busy: bool) -> None:
        self.start_button.setEnabled(not busy and not bool(self.sessions))
        self.stop_button.setEnabled(not busy and bool(self.sessions))
        self.refresh_button.setEnabled(not busy)
        self.add_button.setEnabled(not busy and not bool(self.sessions))
        self.remove_button.setEnabled(not busy and not bool(self.sessions))

    def _operation_failed(self, message: str) -> None:
        self._set_busy(False)
        self.monitor_label.setText("● 操作失败")
        self.monitor_label.setStyleSheet("color: #c53b45;")
        self.append_log(message)
        QMessageBox.critical(self, "监控操作失败", message)

    def _candidate_captures(self, symbol: str) -> list[Path]:
        live = self.capture_dir / f"wind_tbapi_live_{safe_code(symbol)}.json"
        candidates = [live] if live.exists() else []
        try:
            candidates.extend(
                sorted(
                    self.capture_dir.glob("wind_tbapi_probe_sub_*.json"),
                    key=lambda path: path.stat().st_mtime_ns,
                    reverse=True,
                )
            )
        except PermissionError:
            pass
        return candidates

    def _read_symbol_data(self, symbol: str) -> tuple[dict[str, Any], float]:
        for path in self._candidate_captures(symbol):
            try:
                with path.open("r", encoding="utf-8") as handle:
                    capture = json.load(handle)
                decoded = decode_probe_capture(capture)
                for values in decoded["rows"]:
                    if str(values.get("windcode", "")).upper() == symbol:
                        timestamp_ms = capture.get("callback_epoch_ms")
                        timestamp = (
                            float(timestamp_ms) / 1000.0
                            if timestamp_ms is not None
                            else path.stat().st_mtime
                        )
                        return values, timestamp
            except (OSError, json.JSONDecodeError, FrameFormatError):
                continue
        raise ProbeError(f"尚无 {display_symbol(symbol)} 数据")

    @staticmethod
    def _format_number(value: Any, show_wan: bool = False) -> str:
        if not isinstance(value, (int, float)):
            return "—" if value is None else str(value)
        base = f"{value:,.0f}"
        if show_wan and value and value % 10000 == 0:
            return f"{base} ({value / 10000:g}万)"
        return base

    @staticmethod
    def _format_signed(value: Any, show_wan: bool = False) -> str:
        if not isinstance(value, (int, float)):
            return "—" if value is None else str(value)
        if value == 0:
            return "0"
        sign = "+" if value > 0 else "-"
        magnitude = abs(value)
        base = f"{sign}{magnitude:,.0f}"
        if show_wan and magnitude % 10000 == 0:
            return f"{base} ({sign}{magnitude / 10000:g}万)"
        return base

    def _detect_changes(
        self, symbol: str, values: dict[str, Any]
    ) -> tuple[list[str], set[str]]:
        current = {
            "etfbuynumber": values.get("etfbuynumber"),
            "etfbuyamount": values.get("etfbuyamount"),
            "etfsellnumber": values.get("etfsellnumber"),
            "etfsellamount": values.get("etfsellamount"),
            "netamount": values.get("netamount"),
        }
        previous = self.last_values.get(symbol)
        self.last_values[symbol] = current
        if previous is None:
            return [], set()
        changes: list[str] = []
        changed_keys: set[str] = set()
        for key, label, show_wan, signed in ALERT_METRICS:
            old = previous.get(key)
            new = current.get(key)
            if old != new:
                formatter = self._format_signed if signed else self._format_number
                changes.append(
                    f"{label} {formatter(old, show_wan)} → "
                    f"{formatter(new, show_wan)}"
                )
                changed_keys.add(key)
        return changes, changed_keys

    def refresh_data(self) -> None:
        alert_messages: list[str] = []
        fresh_count = 0
        for symbol in self.symbols:
            row = self._row_for_symbol(symbol)
            for column in range(2, 7):
                self.table.item(row, column).setBackground(Qt.GlobalColor.white)
            try:
                values, timestamp = self._read_symbol_data(symbol)
            except ProbeError:
                if symbol not in self.sessions:
                    self.table.item(row, 1).setText("暂无数据")
                continue

            buy_amount = values.get("etfbuyamount")
            sell_amount = values.get("etfsellamount")
            if isinstance(buy_amount, (int, float)) and isinstance(
                sell_amount, (int, float)
            ):
                values["netamount"] = buy_amount - sell_amount
            else:
                values["netamount"] = None

            changes, changed_keys = self._detect_changes(symbol, values)
            column_metrics = [
                (2, "etfbuynumber", False),
                (3, "etfbuyamount", True),
                (4, "etfsellnumber", False),
                (5, "etfsellamount", True),
            ]
            for column, key, show_wan in column_metrics:
                self.table.item(row, column).setText(
                    self._format_number(values.get(key), show_wan)
                )
                if key in changed_keys:
                    self.table.item(row, column).setBackground(
                        Qt.GlobalColor.yellow
                    )

            net_value = values.get("netamount")
            net_item = self.table.item(row, 6)
            net_item.setText(self._format_signed(net_value, True))
            if isinstance(net_value, (int, float)) and net_value > 0:
                net_item.setForeground(QBrush(QColor("#168553")))
            elif isinstance(net_value, (int, float)) and net_value < 0:
                net_item.setForeground(QBrush(QColor("#c53b45")))
            else:
                net_item.setForeground(QBrush(QColor("#526074")))
            if {"etfbuyamount", "etfsellamount"} & changed_keys:
                net_item.setBackground(Qt.GlobalColor.yellow)

            age = max(0.0, time.time() - timestamp)
            stale_after = max(self.interval_spin.value() * 3, 10.0)
            if age <= stale_after:
                fresh_count += 1
                age_text = f"{age:.1f} 秒前"
            else:
                age_text = f"缓存 {age:.0f} 秒"
            self.table.item(row, 7).setText(age_text)
            if changes:
                summary = "；".join(changes)
                self.table.item(row, 8).setText(summary)
                alert_messages.append(f"{display_symbol(symbol)}：{summary}")
                self.append_log(f"变化 {display_symbol(symbol)}：{summary}")
            elif self.table.item(row, 8).text() == "—":
                self.table.item(row, 8).setText("已建立基线")

        if alert_messages:
            self._dispatch_alerts(alert_messages)
        self.change_label.setText(
            f"本轮 {len(alert_messages)} 个标的变化 · {fresh_count} 个数据最新"
        )

    def _dispatch_alerts(self, messages: list[str]) -> None:
        self.last_alert_messages = list(messages)
        combined = "\n".join(messages[:8])
        if len(messages) > 8:
            combined += f"\n另有 {len(messages) - 8} 个标的发生变化"
        if self.sound_check.isChecked():
            QApplication.beep()
        if self.popup_check.isChecked():
            popup = AlertPopup(combined)
            self.alert_popups.append(popup)
            popup.finished.connect(lambda _result, item=popup: self._remove_popup(item))
            popup.show()
            QTimer.singleShot(12000, popup.close)

    def _remove_popup(self, popup: AlertPopup) -> None:
        if popup in self.alert_popups:
            self.alert_popups.remove(popup)

    def closeEvent(self, event: Any) -> None:
        self._save_settings()
        if self.sessions:
            result = QMessageBox.question(
                self,
                "监控仍在运行",
                "关闭界面不会自动停止进程内订阅。是否仍要退出？",
            )
            if result != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ETF 变化监控")
    app.setOrganizationName("ETFDelivery")
    font = QFont()
    font.setPointSize(13)
    app.setFont(font)
    window = MonitorWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
