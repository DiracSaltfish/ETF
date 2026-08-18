from __future__ import annotations

import csv
import hashlib
import os
import sys
from dataclasses import replace
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Callable

from PyQt5.QtCore import QDate, QObject, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import india_engine
from india_calendar import OFFICIAL_SZSE_HOLIDAYS_BY_YEAR, parse_holidays
from india_config import IndiaConfig, load_json_config, save_json_config
from india_models import IndiaCalculation, RedemptionEvent
from india_order_planner import (
    build_inda_close_plan,
    build_swap_plan,
    plan_display_rows,
    validate_live_plan,
    validate_preview_only,
)
from india_sources import (
    load_ib_india_fills,
    load_position_snapshots,
    load_qmt_accounts,
    load_redemption_statement,
)
from india_store import IndiaStore
from india_tws_orders import IndiaTwsOrderClient
import redemption_ui as classic


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
DATA_PATH = ROOT / "data" / "india_redemption.sqlite3"


def qdate_value(value: date) -> QDate:
    return QDate(value.year, value.month, value.day)


def python_date(value: QDate) -> date:
    return date(value.year(), value.month(), value.day())


def fmt(value: object, *, money: bool = False) -> str:
    if value is None or value == "":
        return "--"
    if isinstance(value, Decimal):
        return f"{value:,.2f}" if money else f"{value:,}"
    if isinstance(value, int):
        return f"{value:,}"
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def parse_optional_decimal(value: str) -> Decimal | None:
    text = value.strip().replace(",", "")
    if not text:
        return None
    try:
        return Decimal(text)
    except InvalidOperation:
        raise ValueError(f"金额或净值格式无效：{value}") from None


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.deleteLater()


def color_table_row(table: QTableWidget, row: int, background: str, foreground: str) -> None:
    for column in range(table.columnCount()):
        item = table.item(row, column)
        if item is not None:
            item.setBackground(QColor(background))
            item.setForeground(QColor(foreground))


class IndiaSettingsDialog(QDialog):
    def __init__(self, values: dict[str, object], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("数据源设置")
        self.resize(880, 650)
        self.position_root = classic.DirectoryPicker(
            "选择持仓快照根目录",
            str(values.get("position_root") or "/Users/ellis/Desktop/交易表格"),
        )
        self.qmt1 = classic.FilePicker("选择 QMT1 成交明细", str(values.get("qmt1_path") or ""), "表格 (*.xlsx *.xls *.csv)")
        self.qmt2 = classic.FilePicker("选择 QMT2 成交明细", str(values.get("qmt2_path") or ""), "表格 (*.xlsx *.xls *.csv)")
        self.qmt3 = classic.FilePicker("选择 QMT3 成交明细", str(values.get("qmt3_path") or ""), "表格 (*.xlsx *.xls *.csv)")
        self.redemption_statement = classic.FilePicker(
            "选择 164824 赎回交割单",
            str(values.get("redemption_statement_path") or ""),
            "表格 (*.xlsx *.xls *.csv)",
        )
        self.ib_mode = QComboBox()
        self.ib_mode.addItem("IB Flex 自动缓存", "flex_auto")
        self.ib_mode.addItem("本地 IB CSV", "local_csv")
        configured_mode = str(values.get("ib_data_source_mode") or "flex_auto")
        self.ib_mode.setCurrentIndex(max(0, self.ib_mode.findData(configured_mode)))
        self.ib = classic.FilePicker("选择 IB Flex 活动 CSV", str(values.get("ib_path") or ""), "CSV (*.csv)")
        self.ib_cache = classic.DirectoryPicker(
            "选择 IB 自动缓存目录",
            str(values.get("ib_flex_cache_dir") or (ROOT / "ib_auto_data")),
        )
        self.flex_query_id = QLineEdit(str(values.get("ib_flex_query_id") or ""))
        self.flex_token = QLineEdit(str(values.get("ib_flex_token") or ""))
        self.flex_token.setEchoMode(QLineEdit.Password)
        self.flex_start = QDateEdit()
        self.flex_start.setCalendarPopup(True)
        self.flex_start.setDisplayFormat("yyyy-MM-dd")
        try:
            start = date.fromisoformat(str(values.get("ib_flex_start_date") or "2026-01-01"))
        except ValueError:
            start = date(2026, 1, 1)
        self.flex_start.setDate(qdate_value(start))
        self.qmt_time_root = classic.DirectoryPicker(
            "选择 QMT 成交时间提示目录",
            str(values.get("qmt_time_root") or values.get("shared_folder_path") or ""),
        )
        self.tws_host = QLineEdit(str(values.get("tws_host") or "127.0.0.1"))
        self.tws_port = QSpinBox()
        self.tws_port.setRange(1, 65535)
        self.tws_port.setValue(int(values.get("tws_port") or 7496))
        self.tws_client_id = QSpinBox()
        self.tws_client_id.setRange(0, 2_147_483_647)
        self.tws_client_id.setValue(int(values.get("tws_client_id") or 8888))
        self.tws_account = QLineEdit(str(values.get("tws_account") or ""))
        self.tws_account.setPlaceholderText("单账户可留空；多账户必须填写")
        self.tws_auto_client_id = QCheckBox("client ID占用时自动切换")
        self.tws_auto_client_id.setChecked(bool(values.get("tws_auto_client_id", True)))
        self.live_enabled = QCheckBox("允许本程序向TWS发送实盘订单")
        self.live_enabled.setChecked(bool(values.get("live_enabled", False)))
        self.live_enabled.setStyleSheet("color:#b91c1c; font-weight:700;")
        note = QLabel(
            "持仓数量自动读取 YYYYMMDD/chicang1、2、3.csv；QMT成交明细只用于FIFO成本。"
            "程序启动和重新计算只读本地IB缓存，只有点击主界面的“拉取IB”才联网。"
        )
        note.setWordWrap(True)
        note.setObjectName("calculationGuide")
        form = QFormLayout()
        form.addRow("持仓快照根目录", self.position_root)
        form.addRow("QMT1 成交明细（成本，可空）", self.qmt1)
        form.addRow("QMT2 成交明细（成本，可空）", self.qmt2)
        form.addRow("QMT3 成交明细（成本，可空）", self.qmt3)
        form.addRow("QMT成交时间提示目录", self.qmt_time_root)
        form.addRow("164824赎回交割单", self.redemption_statement)
        form.addRow("IB数据输入方式", self.ib_mode)
        form.addRow("本地IB CSV", self.ib)
        form.addRow("IB自动缓存目录", self.ib_cache)
        form.addRow("Flex起始日期", self.flex_start)
        form.addRow("Flex Query ID", self.flex_query_id)
        form.addRow("Flex Token", self.flex_token)
        form.addRow("TWS主机", self.tws_host)
        form.addRow("TWS端口", self.tws_port)
        form.addRow("TWS首选Client ID", self.tws_client_id)
        form.addRow("IB交易账户", self.tws_account)
        form.addRow("TWS连接", self.tws_auto_client_id)
        form.addRow("实盘总开关", self.live_enabled)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> dict[str, object]:
        return {
            "position_root": self.position_root.value(),
            "qmt1_path": self.qmt1.value(),
            "qmt2_path": self.qmt2.value(),
            "qmt3_path": self.qmt3.value(),
            "qmt_time_root": self.qmt_time_root.value(),
            "redemption_statement_path": self.redemption_statement.value(),
            "ib_data_source_mode": str(self.ib_mode.currentData()),
            "ib_path": self.ib.value(),
            "ib_flex_cache_dir": self.ib_cache.value(),
            "ib_flex_start_date": self.flex_start.date().toString("yyyy-MM-dd"),
            "ib_flex_query_id": self.flex_query_id.text().strip(),
            "ib_flex_token": self.flex_token.text().strip(),
            "tws_host": self.tws_host.text().strip() or "127.0.0.1",
            "tws_port": self.tws_port.value(),
            "tws_client_id": self.tws_client_id.value(),
            "tws_account": self.tws_account.text().strip(),
            "tws_auto_client_id": self.tws_auto_client_id.isChecked(),
            "live_enabled": self.live_enabled.isChecked(),
        }


class FundHolidayDialog(QDialog):
    def __init__(self, values: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("基金暂停开放日")
        self.resize(620, 460)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.list_widget = QListWidget()
        for value in sorted(set(values)):
            self.list_widget.addItem(value)
        add_button = QPushButton("添加所选日期")
        remove_button = QPushButton("删除选中日期")
        add_button.clicked.connect(self.add_selected)
        remove_button.clicked.connect(self.remove_selected)
        note = QLabel("深交所官方休市日已经内置；这里只维护164824因境外市场或基金公告产生的额外暂停申赎日期。")
        note.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(add_button)
        controls.addWidget(remove_button)
        controls.addStretch(1)
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self.calendar)
        splitter.addWidget(self.list_widget)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(splitter, 1)
        layout.addLayout(controls)
        layout.addWidget(buttons)

    def add_selected(self) -> None:
        value = self.calendar.selectedDate().toString("yyyy-MM-dd")
        existing = {self.list_widget.item(index).text() for index in range(self.list_widget.count())}
        if value not in existing:
            self.list_widget.addItem(value)
            self.list_widget.sortItems()

    def remove_selected(self) -> None:
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))

    def values(self) -> list[str]:
        return [self.list_widget.item(index).text() for index in range(self.list_widget.count())]


class ManualRedemptionDialog(QDialog):
    def __init__(self, calculation_day: date, snapshots: dict[str, dict[str, object]], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("手工登记164824赎回")
        self.resize(520, 390)
        self.snapshots = snapshots
        self.day = QDateEdit(qdate_value(calculation_day))
        self.day.setCalendarPopup(True)
        self.day.setDisplayFormat("yyyy-MM-dd")
        self.account = QComboBox()
        self.account.addItems(["QMT1", "QMT2", "QMT3"])
        self.qty = QSpinBox()
        self.qty.setRange(1, 100_000_000)
        self.qty.setSingleStep(270_000)
        self.qty.setValue(270_000)
        self.contract = QLineEdit()
        self.net = QLineEdit()
        self.nav = QLineEdit()
        self.available = QLabel()
        self.available.setObjectName("sourceHint")
        self.account.currentTextChanged.connect(self.update_available)
        self.update_available()
        form = QFormLayout()
        form.addRow("赎回日期", self.day)
        form.addRow("账户", self.account)
        form.addRow("当前最终可赎", self.available)
        form.addRow("赎回份额", self.qty)
        form.addRow("合同号（可选）", self.contract)
        form.addRow("实际净赎回款（可选）", self.net)
        form.addRow("单位净值（可选）", self.nav)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def update_available(self) -> None:
        value = int(self.snapshots.get(self.account.currentText(), {}).get("eligible_qty", 0))
        self.available.setText(f"{value:,} 份")


class PreviewPlanTab(QWidget):
    CHECK_COLUMN = 0
    STATUS_COLUMN = 10
    ORDER_ID_COLUMN = 11

    def __init__(
        self,
        config_provider: Callable[[], IndiaConfig],
        store: IndiaStore,
        kind: str,
        tws_client: IndiaTwsOrderClient,
    ) -> None:
        super().__init__()
        self.config_provider = config_provider
        self.store = store
        self.kind = kind
        self.tws_client = tws_client
        self.specs_by_ref: dict[str, object] = {}
        self.rows_by_ref: dict[str, int] = {}
        self.sent_refs: set[str] = set()
        self.preview_valid = False
        self.connected = tws_client.is_connected()

        self.trade_day = QDateEdit(qdate_value(date.today()))
        self.trade_day.setCalendarPopup(True)
        self.trade_day.setDisplayFormat("yyyy-MM-dd")
        self.quantity = QSpinBox()
        self.quantity.setRange(1, 10_000_000)
        self.override = QLineEdit()
        self.override.setPlaceholderText("例如 NIFTYQ26")
        self.generate_button = QPushButton("生成订单预览（不发送）")
        self.connect_button = QPushButton("连接IB交易接口")
        self.disconnect_button = QPushButton("断开IB")
        self.connection_status = QLabel("IB未连接")
        self.connection_status.setObjectName("sourceHint")
        self.disconnect_button.setEnabled(self.connected)
        self.connect_button.setEnabled(not self.connected)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("交易日（美东）"))
        controls.addWidget(self.trade_day)
        if kind == "swap":
            self.quantity.setMaximum(10_000)
            self.quantity.setValue(1)
            controls.addWidget(QLabel("篮子数"))
            controls.addWidget(self.quantity)
            controls.addWidget(QLabel("NIFTY合约覆盖（可空）"))
            controls.addWidget(self.override)
        else:
            self.quantity.setMinimum(2)
            self.quantity.setValue(970)
            controls.addWidget(QLabel("实际INDA空头股数"))
            controls.addWidget(self.quantity)
        controls.addWidget(self.generate_button)
        controls.addSpacing(14)
        controls.addWidget(self.connect_button)
        controls.addWidget(self.disconnect_button)
        controls.addWidget(self.connection_status, 1)

        warning_text = (
            "实盘风险提示：换仓发送为 NIFTY BUY MKT 时间条件父单 + INDA SELL MKT附加子单；"
            "只有NIFTY完全成交后INDA才激活。"
            if kind == "swap"
            else "实盘风险提示：发送的是 INDA BUY MKT DAY 时间条件单；发送前会核对TWS空头持仓，防止反向开多。"
        )
        warning = QLabel(
            warning_text
            + " 市价单没有价格保护。生成预览不会下单；勾选、解锁并输入SEND确认后才会进入TWS队列。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background:#fff1f2; color:#9f1239; border:1px solid #fecdd3; "
            "border-radius:8px; padding:8px; font-weight:600;"
        )

        self.table = classic.configured_table()
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.unlock_checkbox = QCheckBox("解锁实盘下单（仍需输入SEND确认）")
        self.unlock_checkbox.setStyleSheet("color:#b91c1c; font-weight:700;")
        self.send_button = QPushButton("发送勾选订单到TWS（实盘）")
        self.send_button.setEnabled(False)
        self.send_button.setStyleSheet(
            "QPushButton:enabled { background:#b91c1c; color:white; font-weight:700; }"
        )
        self.cancel_button = QPushButton("撤销勾选TWS订单")
        self.cancel_button.setEnabled(False)
        send_controls = QHBoxLayout()
        send_controls.addWidget(self.unlock_checkbox)
        send_controls.addWidget(self.send_button)
        send_controls.addWidget(self.cancel_button)
        send_controls.addStretch(1)
        send_controls.addWidget(QLabel("固定参数：MKT / DAY / outsideRth=false / INDIA_ orderRef"))

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(125)
        self.log.setPlaceholderText("TWS连接、openOrder、orderStatus、成交和佣金回报将在这里显示。")
        log_box = QGroupBox("TWS订单回报")
        log_layout = QVBoxLayout(log_box)
        log_layout.addWidget(self.log)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(controls)
        layout.addWidget(warning)
        layout.addWidget(self.table, 1)
        layout.addLayout(send_controls)
        layout.addWidget(log_box)

        self.generate_button.clicked.connect(self.generate)
        self.connect_button.clicked.connect(self.tws_client.connect_tws)
        self.disconnect_button.clicked.connect(self.tws_client.disconnect_tws)
        self.unlock_checkbox.toggled.connect(self.update_send_button)
        self.send_button.clicked.connect(self.send_selected_orders)
        self.cancel_button.clicked.connect(self.cancel_selected_orders)
        self.table.itemChanged.connect(self.update_send_button)
        self.trade_day.dateChanged.connect(self.invalidate_preview)
        self.quantity.valueChanged.connect(self.invalidate_preview)
        self.override.textChanged.connect(self.invalidate_preview)
        self.tws_client.statusChanged.connect(self.update_connection_status)
        self.tws_client.orderEvent.connect(self.handle_order_event)

    def invalidate_preview(self, *_args) -> None:
        if not self.specs_by_ref:
            return
        self.preview_valid = False
        for row in self.rows_by_ref.values():
            item = self.table.item(row, self.STATUS_COLUMN)
            if item is not None:
                item.setText("参数已变化，请重新生成")
        self.update_send_button()

    def generate(self) -> None:
        try:
            config = self.config_provider()
            day = python_date(self.trade_day.date())
            if self.kind == "swap":
                specs = list(
                    build_swap_plan(
                        day,
                        self.quantity.value(),
                        config,
                        nifty_override=self.override.text().strip() or None,
                    )
                )
            else:
                specs = list(build_inda_close_plan(day, self.quantity.value(), config))
            specs = [
                replace(item, order_ref=self.store.next_order_ref(item.order_ref))
                for item in specs
            ]
            validate_preview_only(config, specs)
            display_rows = plan_display_rows(specs)
            rows = [["", *row, "待发送", "--"] for row in display_rows]
            headers = [
                "发送", "序号", "标的", "方向", "数量", "触发时间（美东）", "北京时间",
                "合约月", "用途", "OrderRef", "状态", "IB订单号",
            ]
            self.table.blockSignals(True)
            classic.fill_table(self.table, headers, rows)
            self.specs_by_ref = {item.order_ref: item for item in specs}
            self.rows_by_ref = {item.order_ref: row for row, item in enumerate(specs)}
            self.sent_refs = self.store.sent_order_refs()
            for row, spec in enumerate(specs):
                check = QTableWidgetItem("")
                check.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable)
                already_sent = spec.order_ref in self.sent_refs
                check.setCheckState(Qt.Unchecked if already_sent else Qt.Checked)
                self.table.setItem(row, self.CHECK_COLUMN, check)
                history = self.store.list_order_events(spec.order_ref)
                if history:
                    latest = history[-1]
                    self.table.item(row, self.STATUS_COLUMN).setText(
                        str(latest.get("status") or latest.get("event_type") or "已提交")
                    )
                    if latest.get("order_id") is not None:
                        self.table.item(row, self.ORDER_ID_COLUMN).setText(str(latest["order_id"]))
            self.table.blockSignals(False)
            self.store.save_order_specs(specs)
            self.preview_valid = True
            live_text = "实盘总开关已开启" if config.live_enabled else "实盘总开关仍关闭"
            self.log.appendPlainText(
                f"{datetime.now():%H:%M:%S} 已生成 {len(specs)} 条预览并保存；没有发送。{live_text}。"
            )
            self.update_send_button()
        except Exception as exc:
            self.preview_valid = False
            self.specs_by_ref.clear()
            self.rows_by_ref.clear()
            classic.fill_table(self.table, ["状态"], [[str(exc)]])
            self.update_send_button()
            QMessageBox.warning(self, "无法生成订单预览", str(exc))

    def selected_specs(self) -> list[object]:
        selected = []
        for order_ref, row in self.rows_by_ref.items():
            if order_ref in self.sent_refs:
                continue
            check = self.table.item(row, self.CHECK_COLUMN)
            if check is not None and check.checkState() == Qt.Checked:
                selected.append(self.specs_by_ref[order_ref])
        return sorted(selected, key=lambda item: item.sequence)

    def checked_order_refs(self) -> list[str]:
        refs = []
        for order_ref, row in self.rows_by_ref.items():
            check = self.table.item(row, self.CHECK_COLUMN)
            if check is not None and check.checkState() == Qt.Checked:
                refs.append(order_ref)
        return refs

    def update_send_button(self, *_args) -> None:
        try:
            live_enabled = self.config_provider().live_enabled
        except Exception:
            live_enabled = False
        enabled = bool(
            live_enabled
            and self.connected
            and self.preview_valid
            and self.unlock_checkbox.isChecked()
            and self.selected_specs()
            and all(getattr(item, "live_allowed", False) for item in self.selected_specs())
        )
        self.send_button.setEnabled(enabled)
        cancel_refs = [item for item in self.checked_order_refs() if item in self.sent_refs]
        self.cancel_button.setEnabled(bool(self.connected and cancel_refs))

    def update_connection_status(self, text: str, connected: bool) -> None:
        self.connected = connected
        self.connection_status.setText(text)
        busy = self.tws_client.is_busy()
        self.connect_button.setEnabled(not connected and not busy)
        self.disconnect_button.setEnabled(connected or busy)
        self.update_send_button()

    def send_selected_orders(self) -> None:
        try:
            config = self.config_provider()
            specs = validate_live_plan(config, self.selected_specs())
            if self.kind == "swap" and len(specs) != len(self.specs_by_ref):
                raise ValueError("换仓父子单不可拆分发送，请同时勾选NIFTY和INDA")
            summary = "\n".join(
                f"{item.action} {item.quantity:,} {item.symbol} @ {item.trigger_time_et}"
                for item in specs
            )
            typed, accepted = QInputDialog.getText(
                self,
                "确认发送实盘订单",
                f"以下订单将发送到TWS：\n\n{summary}\n\n请输入 SEND 确认：",
            )
            if not accepted or typed.strip().upper() != "SEND":
                self.log.appendPlainText(f"{datetime.now():%H:%M:%S} 用户取消发送；没有订单进入TWS。")
                return
            if not self.tws_client.submit_confirmed_batch(specs):
                raise RuntimeError("订单未能进入TWS发送队列")
            self.unlock_checkbox.setChecked(False)
        except Exception as exc:
            QMessageBox.warning(self, "订单未发送", str(exc))

    def cancel_selected_orders(self) -> None:
        refs = [item for item in self.checked_order_refs() if item in self.sent_refs]
        if not refs:
            QMessageBox.information(self, "没有可撤订单", "请勾选已经提交到TWS的订单。")
            return
        sent_in_plan = [item for item in self.rows_by_ref if item in self.sent_refs]
        if self.kind == "swap" and set(refs) != set(sent_in_plan):
            QMessageBox.warning(
                self,
                "换仓撤单不可拆分",
                "请同时勾选本次换仓的NIFTY父单和INDA子单；TWS会分别回报各腿最终状态。",
            )
            return
        answer = QMessageBox.question(
            self,
            "确认撤单",
            "将向TWS提交以下撤单请求：\n\n" + "\n".join(refs) + "\n\n是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not self.tws_client.request_cancel(refs):
            QMessageBox.warning(self, "撤单请求未提交", "请检查IB连接和订单状态。")

    def handle_order_event(self, payload: object) -> None:
        values = dict(payload)
        order_ref = str(values.get("order_ref") or "")
        if order_ref and order_ref not in self.rows_by_ref:
            return
        event = str(values.get("event") or "event")
        status = str(values.get("status") or "")
        message = str(values.get("message") or "")
        order_id = values.get("order_id")
        self.log.appendPlainText(
            f"{datetime.now():%H:%M:%S} {event} {order_ref or '--'} "
            f"{status} {message}".strip()
        )
        row = self.rows_by_ref.get(order_ref)
        if row is None:
            return
        status_item = self.table.item(row, self.STATUS_COLUMN)
        order_id_item = self.table.item(row, self.ORDER_ID_COLUMN)
        if status_item is not None:
            status_item.setText(status or event)
        if order_id_item is not None and order_id is not None:
            order_id_item.setText(str(order_id))
        if event in {
            "queued", "submitted", "openOrder", "orderStatus", "execDetails",
            "cancelQueued", "cancelRequested",
        }:
            check = self.table.item(row, self.CHECK_COLUMN)
            if check is not None:
                self.table.blockSignals(True)
                check.setCheckState(Qt.Unchecked)
                self.table.blockSignals(False)
        if event in {
            "submitted", "openOrder", "orderStatus", "execDetails", "commissionReport", "sync"
        }:
            self.sent_refs.add(order_ref)
        self.update_send_button()


class IndiaMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ETF 赎回收益计算器 V3 · 印度164824")
        self.resize(1420, 900)
        self.raw_config = load_json_config(CONFIG_PATH)
        self.config = IndiaConfig.from_mapping(self.raw_config)
        self.store = IndiaStore(DATA_PATH)
        self.tws_client = IndiaTwsOrderClient(
            str(self.raw_config.get("tws_host") or "127.0.0.1"),
            int(self.raw_config.get("tws_port") or 7496),
            int(self.raw_config.get("tws_client_id") or 8888),
            account=str(self.raw_config.get("tws_account") or ""),
            auto_client_id=bool(self.raw_config.get("tws_auto_client_id", True)),
            parent=self,
        )
        self.tws_client.seed_submitted_order_refs(self.store.sent_order_refs())
        self.tws_client.orderEvent.connect(self.persist_order_event)
        self.result: IndiaCalculation | None = None
        self.refreshing = False
        self._ib_thread: QThread | None = None
        self._ib_worker: QObject | None = None

        self.fx_spin = QDoubleSpinBox()
        self.fx_spin.setDecimals(6)
        self.fx_spin.setRange(0.000001, 1000)
        self.fx_spin.setValue(float(self.raw_config.get("fx_rate") or 6.8))
        self.fx_spin.setSingleStep(0.0001)
        self.calculation_day = QDateEdit(qdate_value(date.today()))
        self.calculation_day.setCalendarPopup(True)
        self.calculation_day.setDisplayFormat("yyyy-MM-dd")
        self.source_label = QLabel()
        self.source_label.setObjectName("sourceHint")
        self.source_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.source_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.settings_button = QPushButton("数据源")
        self.pull_ib_button = QPushButton("拉取IB")
        self.holiday_button = QPushButton("休市日")
        self.detail_button = QPushButton("显示详细")
        self.detail_button.setCheckable(True)
        self.export_button = QPushButton("导出CSV")
        self.refresh_button = QPushButton("重新计算")
        self.status_label = QLabel("等待读取")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.summary_group = QGroupBox("概览")
        self.summary_grid = QGridLayout(self.summary_group)
        self.summary_grid.setContentsMargins(6, 6, 6, 6)
        self.summary_grid.setHorizontalSpacing(6)

        self.basket_table = classic.configured_table()
        self.position_table = classic.configured_table()
        self.mapping_table = classic.configured_table()
        self.redemption_table = classic.configured_table()
        self.settlement_table = classic.configured_table()
        self.cash_table = classic.configured_table()
        self.calendar_table = classic.configured_table()
        self.warning_table = classic.configured_table()
        self.swap_tab = PreviewPlanTab(self.current_config, self.store, "swap", self.tws_client)
        self.close_tab = PreviewPlanTab(self.current_config, self.store, "close", self.tws_client)

        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(8, 8, 8, 8)
        overview_actions = QHBoxLayout()
        self.manual_button = QPushButton("登记当日赎回")
        self.import_button = QPushButton("导入赎回交割单")
        self.overview_hint = QLabel("270,000份 = 1张NIFTY + 970股INDA；未取得实单前费率保持待确认")
        self.overview_hint.setObjectName("sourceHint")
        overview_actions.addWidget(self.manual_button)
        overview_actions.addWidget(self.import_button)
        overview_actions.addWidget(self.overview_hint, 1)
        overview_layout.addLayout(overview_actions)
        overview_layout.addWidget(self.basket_table, 1)

        redemption_panel = QWidget()
        redemption_layout = QVBoxLayout(redemption_panel)
        redemption_layout.setContentsMargins(8, 8, 8, 8)
        redemption_actions = QHBoxLayout()
        self.delete_redemption_button = QPushButton("删除选中赎回登记")
        redemption_note = QLabel("用于纠正误录或错误导入；删除后会立即重算赎回预留与可赎数量。")
        redemption_note.setObjectName("sourceHint")
        redemption_actions.addWidget(self.delete_redemption_button)
        redemption_actions.addWidget(redemption_note, 1)
        redemption_layout.addLayout(redemption_actions)
        redemption_layout.addWidget(self.redemption_table, 1)

        tabs = QTabWidget()
        tabs.addTab(overview, "篮子汇总")
        tabs.addTab(self.mapping_table, "篮子配对图")
        tabs.addTab(self.position_table, "持仓与可赎")
        tabs.addTab(redemption_panel, "赎回登记")
        tabs.addTab(self.settlement_table, "到账与结算")
        tabs.addTab(self.swap_tab, "NIFTY换仓计划")
        tabs.addTab(self.close_tab, "INDA分段平仓")
        tabs.addTab(self.cash_table, "资金流水")
        tabs.addTab(self.calendar_table, "交易日历")
        tabs.addTab(self.warning_table, "异常与未匹配")
        tabs.setMinimumWidth(0)
        tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.tabs = tabs

        root = QWidget()
        layout = QVBoxLayout(root)
        layout.addWidget(self.header())
        layout.addWidget(self.summary_group)
        layout.addWidget(tabs, 1)
        layout.addWidget(self.status_label)
        self.setCentralWidget(root)

        self.settings_button.clicked.connect(self.open_settings)
        self.pull_ib_button.clicked.connect(self.pull_ib)
        self.holiday_button.clicked.connect(self.open_holidays)
        self.detail_button.toggled.connect(self.toggle_details)
        self.export_button.clicked.connect(self.export_csv)
        self.refresh_button.clicked.connect(self.calculate)
        self.manual_button.clicked.connect(self.add_manual_redemption)
        self.import_button.clicked.connect(self.import_statement)
        self.delete_redemption_button.clicked.connect(self.delete_selected_redemption)
        self.fx_spin.editingFinished.connect(self.change_fx)
        self.calculation_day.dateChanged.connect(lambda _value: self.schedule_calculation())
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(250)
        self.refresh_timer.timeout.connect(self.calculate)
        self.update_source_label()
        QTimer.singleShot(0, self.calculate)

    def header(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(QLabel("全局 USD/CNH"))
        layout.addWidget(self.fx_spin)
        layout.addWidget(QLabel("计算日"))
        layout.addWidget(self.calculation_day)
        layout.addWidget(self.source_label, 1)
        layout.addWidget(self.settings_button)
        layout.addWidget(self.pull_ib_button)
        layout.addWidget(self.holiday_button)
        layout.addWidget(self.detail_button)
        layout.addWidget(self.export_button)
        layout.addWidget(self.refresh_button)
        return bar

    def current_config(self) -> IndiaConfig:
        values = dict(self.raw_config)
        values["fx_rate"] = f"{self.fx_spin.value():.6f}"
        self.config = IndiaConfig.from_mapping(values)
        return self.config

    def schedule_calculation(self) -> None:
        self.refresh_timer.start()

    def change_fx(self) -> None:
        self.raw_config["fx_rate"] = f"{self.fx_spin.value():.6f}"
        save_json_config(CONFIG_PATH, self.raw_config)
        self.calculate()

    def open_settings(self) -> None:
        dialog = IndiaSettingsDialog(self.raw_config, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        updates = dialog.values()
        live_changed = bool(updates.get("live_enabled")) != bool(self.raw_config.get("live_enabled"))
        if updates.get("live_enabled") and not self.raw_config.get("live_enabled"):
            answer = QMessageBox.question(
                self,
                "开启实盘总开关",
                "开启后，订单页在完成连接、勾选、解锁和SEND确认后可以向TWS发送市价条件单。\n"
                "是否确认开启？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
        connection_keys = ("tws_host", "tws_port", "tws_client_id", "tws_account", "tws_auto_client_id")
        connection_changed = any(self.raw_config.get(key) != updates.get(key) for key in connection_keys)
        if connection_changed and self.tws_client.is_connected():
            QMessageBox.warning(self, "请先断开IB", "修改TWS连接或账户配置前，请先在订单页断开IB。")
            return
        self.raw_config.update(updates)
        if connection_changed:
            self.tws_client.configure(
                host=str(self.raw_config.get("tws_host") or "127.0.0.1"),
                port=int(self.raw_config.get("tws_port") or 7496),
                client_id=int(self.raw_config.get("tws_client_id") or 8888),
                account=str(self.raw_config.get("tws_account") or ""),
                auto_client_id=bool(self.raw_config.get("tws_auto_client_id", True)),
            )
        save_json_config(CONFIG_PATH, self.raw_config)
        self.update_source_label()
        if live_changed:
            self.swap_tab.invalidate_preview()
            self.close_tab.invalidate_preview()
        self.swap_tab.update_send_button()
        self.close_tab.update_send_button()
        self.calculate()

    def persist_order_event(self, payload: object) -> None:
        try:
            self.store.append_order_event(dict(payload))
        except Exception as exc:
            self.status_label.setText(f"TWS回报写入主账失败：{exc}")

    def open_holidays(self) -> None:
        values = self.raw_config.get("fund_closed_days")
        dialog = FundHolidayDialog([str(item) for item in values] if isinstance(values, list) else [], self)
        if dialog.exec_() != QDialog.Accepted:
            return
        self.raw_config["fund_closed_days"] = dialog.values()
        save_json_config(CONFIG_PATH, self.raw_config)
        self.calculate()

    def toggle_details(self, checked: bool) -> None:
        self.detail_button.setText("隐藏详细" if checked else "显示详细")
        if self.result is not None:
            self.populate_baskets()

    def update_source_label(self) -> None:
        position = Path(str(self.raw_config.get("position_root") or "/Users/ellis/Desktop/交易表格"))
        ib_path = self.ib_input_path()
        qmt_names = [
            Path(str(self.raw_config.get(f"qmt{number}_path") or "")).name or "--"
            for number in (1, 2, 3)
        ]
        self.source_label.setText(
            f"持仓 {position.name or position} | QMT {' / '.join(qmt_names)} | IB {Path(ib_path).name if ib_path else '--'}"
        )

    def ib_input_path(self) -> str | None:
        if str(self.raw_config.get("ib_data_source_mode") or "flex_auto") == "flex_auto":
            cache = Path(str(self.raw_config.get("ib_flex_cache_dir") or (ROOT / "ib_auto_data"))).expanduser()
            return str(cache / "ib_activity_auto.csv")
        value = str(self.raw_config.get("ib_path") or "").strip()
        return value or None

    def qmt_paths(self) -> dict[str, str | None]:
        return {
            account: str(self.raw_config.get(f"{account.lower()}_path") or "").strip() or None
            for account in ("QMT1", "QMT2", "QMT3")
        }

    def calculate(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        self.status_label.setText("正在读取164824持仓、赎回主账与IB成交...")
        QApplication.processEvents()
        try:
            config = self.current_config()
            calculation_day = python_date(self.calculation_day.date())
            snapshots = load_position_snapshots(self.raw_config.get("position_root"), config.fund_code)
            records = load_qmt_accounts(
                self.qmt_paths(),
                config.fund_code,
                self.raw_config.get("qmt_time_root") or self.raw_config.get("shared_folder_path") or None,
            )
            events = self.store.list_redemptions()
            ib_path = self.ib_input_path()
            fills = load_ib_india_fills(ib_path)
            self.result = india_engine.calculate(
                records,
                events,
                config,
                fx_rate=Decimal(str(self.fx_spin.value())),
                holidays=parse_holidays(self.raw_config.get("china_market_holidays")),
                fund_closed_days=parse_holidays(self.raw_config.get("fund_closed_days")),
                calendar_years=self.raw_config.get("china_calendar_years") or (),
                ib_fills=fills,
                position_snapshots=snapshots,
                as_of_day=calculation_day,
            )
            self.populate_all()
            self.status_label.setText(
                f"已读取持仓快照 {len(snapshots)} 条 | QMT成本明细 {len(records)} 条 | "
                f"赎回事件 {len(events)} 条 | IB印度成交 {len(fills)} 条 | "
                f"标准篮子 {self.result.standard_basket_count} 个"
            )
        except Exception as exc:
            self.result = None
            self.status_label.setText(f"计算失败：{exc}")
            classic.fill_table(self.warning_table, ["类型", "说明"], [["计算失败", str(exc)]])
        finally:
            self.refreshing = False
            self.update_source_label()

    def populate_all(self) -> None:
        assert self.result is not None
        self.populate_summary()
        self.populate_baskets()
        self.populate_positions()
        self.populate_redemptions()
        self.populate_mapping()
        self.populate_settlements()
        self.populate_cash()
        self.populate_calendar()
        self.populate_warnings()

    def add_summary_field(self, index: int, name: str, value: str, pnl: Decimal | None = None) -> None:
        field = QWidget()
        field.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        if pnl is None:
            field_name, value_name = "summaryField", "summaryValue"
        elif pnl >= 0:
            field_name, value_name = "summaryFieldPositive", "summaryValuePositive"
        else:
            field_name, value_name = "summaryFieldNegative", "summaryValueNegative"
        field.setObjectName(field_name)
        layout = QVBoxLayout(field)
        layout.setContentsMargins(8, 5, 8, 5)
        key = QLabel(name)
        key.setObjectName("summaryKey")
        val = QLabel(value)
        val.setObjectName(value_name)
        val.setWordWrap(True)
        layout.addWidget(key)
        layout.addWidget(val)
        row, column = divmod(index, 5)
        self.summary_grid.addWidget(field, row, column)

    def populate_summary(self) -> None:
        assert self.result is not None
        clear_layout(self.summary_grid)
        baskets = self.result.baskets
        available = [item for item in baskets if item.settlement_status == "available"]
        domestic_values = [item.domestic_pnl for item in baskets if item.domestic_pnl is not None]
        domestic_total = sum(domestic_values, Decimal("0"))
        hedge_total = sum((item.hedge.pnl_cny for item in baskets), Decimal("0"))
        redeemable = sum(int(item.get("eligible_qty", 0)) for item in self.result.account_snapshots.values())
        reservations = sum(int(item.get("reserved_qty", 0)) for item in self.result.account_snapshots.values())
        blocked = sum(item.get("confidence") == "blocked" for item in self.result.account_snapshots.values())
        values = [
            ("篮子数量", f"{len(baskets)}", None),
            ("已可用", f"{len(available)}", None),
            ("合计收益 RMB", fmt(self.result.total_pnl_cny, money=True), self.result.total_pnl_cny),
            ("国内已确认收益 RMB", fmt(domestic_total, money=True), domestic_total if domestic_values else None),
            ("对冲收益 RMB", fmt(hedge_total, money=True), hedge_total),
            ("三账户最终可赎", f"{redeemable:,}", None),
            ("可执行完整篮子", f"{sum(int(item.get('full_baskets', 0)) for item in self.result.account_snapshots.values())}", None),
            ("赎回预留", f"{reservations:,}", None),
            ("IB完整配对", f"{sum(item.hedge_status == 'fully_closed' for item in baskets)}", None),
            ("数据阻断账户", f"{blocked}", None),
        ]
        for index, (name, value, pnl) in enumerate(values):
            self.add_summary_field(index, name, value, pnl)
        for column in range(5):
            self.summary_grid.setColumnStretch(column, 1)

    def populate_baskets(self) -> None:
        assert self.result is not None
        detailed = self.detail_button.isChecked()
        headers = [
            "轮次", "状态", "赎回日", "T+5", "T+6", "账户", "份额", "国内成本", "净赎回款",
            "国内收益", "对冲收益", "合计RMB", "IB映射",
        ]
        if detailed:
            headers.extend(["费用", "金额来源", "NIFTY开/平", "INDA开/平", "数据质量", "提示"])
        rows: list[list[object]] = []
        for basket in self.result.baskets:
            settlement = basket.settlement
            row: list[object] = [
                basket.sequence,
                basket.settlement_status,
                basket.redeem_day.isoformat(),
                settlement.expected_statement_day.isoformat() if settlement else "--",
                settlement.expected_available_day.isoformat() if settlement else "--",
                basket.account,
                f"{basket.redeem_qty:,}",
                fmt(basket.domestic_cost, money=True),
                fmt(basket.domestic_net_amount, money=True),
                fmt(basket.domestic_pnl, money=True),
                fmt(basket.hedge.pnl_cny, money=True),
                fmt(basket.total_pnl_cny, money=True),
                basket.hedge_status,
            ]
            if detailed:
                row.extend(
                    [
                        fmt(settlement.fee_amount if settlement else None, money=True),
                        settlement.amount_source if settlement else "--",
                        f"{basket.hedge.nifty_open_qty}/{basket.hedge.nifty_close_qty}",
                        f"{basket.hedge.inda_open_qty}/{basket.hedge.inda_close_qty}",
                        basket.data_quality,
                        "；".join(basket.warnings) or "--",
                    ]
                )
            rows.append(row)
        classic.fill_table(self.basket_table, headers, rows)
        colors = {
            "available": ("#ecfdf3", "#14532d"),
            "credited_pending_use": ("#fff7ed", "#9a3412"),
            "waiting_statement": ("#fffbeb", "#92400e"),
        }
        for row, basket in enumerate(self.result.baskets):
            background, foreground = colors.get(basket.settlement_status, ("#ffffff", "#111827"))
            if basket.data_quality == "blocked" or basket.hedge_status == "mismatch":
                background, foreground = "#fef2f2", "#991b1b"
            color_table_row(self.basket_table, row, background, foreground)

    def populate_positions(self) -> None:
        assert self.result is not None
        headers = [
            "账户", "最新持仓", "回看交易日", "三日收盘持仓", "三日最小", "赎回预留", "最终可赎",
            "完整篮子", "可执行份额", "零碎份额", "可信度", "最新快照",
        ]
        rows = []
        accounts = ("QMT1", "QMT2", "QMT3")
        for account in accounts:
            item = self.result.account_snapshots.get(account, {})
            rows.append(
                [
                    account,
                    fmt(item.get("total_qty", 0)),
                    " / ".join(day.strftime("%m-%d") for day in item.get("lookback_days", ())) or "--",
                    " / ".join("--" if qty is None else f"{qty:,}" for qty in item.get("lookback_qtys", ())) or "--",
                    fmt(item.get("snapshot_eligible_qty", item.get("eligible_qty", 0))),
                    fmt(item.get("reserved_qty", 0)),
                    fmt(item.get("eligible_qty", 0)),
                    fmt(item.get("full_baskets", 0)),
                    fmt(item.get("executable_qty", 0)),
                    fmt(item.get("residual_qty", 0)),
                    item.get("confidence", "--"),
                    fmt(item.get("last_trade_day")),
                ]
            )
        classic.fill_table(self.position_table, headers, rows)
        for row, account in enumerate(accounts):
            confidence = self.result.account_snapshots.get(account, {}).get("confidence")
            color_table_row(
                self.position_table,
                row,
                "#ecfdf3" if confidence == "confirmed" else "#fef2f2",
                "#14532d" if confidence == "confirmed" else "#991b1b",
            )

    def populate_redemptions(self) -> None:
        events = self.store.list_redemptions()
        rows = [
            [
                item.redeem_day.isoformat(), item.account, f"{item.qty:,}", item.source, item.contract_no or "--",
                fmt(item.gross_amount, money=True), fmt(item.fee_amount, money=True), fmt(item.net_amount, money=True),
                fmt(item.nav_per_share), fmt(item.statement_day), item.event_id,
            ]
            for item in events
        ]
        classic.fill_table(
            self.redemption_table,
            ["赎回日", "账户", "份额", "来源", "合同号", "毛额", "费用", "净款", "净值", "交割单日", "事件ID"],
            rows,
        )

    def populate_mapping(self) -> None:
        assert self.result is not None
        rows = []
        for basket in self.result.baskets:
            hedge = basket.hedge
            rows.append(
                [
                    basket.sequence, basket.redeem_day.isoformat(), basket.account, f"{basket.redeem_qty:,}",
                    basket.nifty_target, f"{hedge.nifty_open_qty}/{hedge.nifty_close_qty}",
                    fmt(hedge.nifty_open_avg), fmt(hedge.nifty_close_avg), fmt(hedge.nifty_pnl_usd, money=True),
                    basket.inda_target, f"{hedge.inda_open_qty}/{hedge.inda_close_qty}",
                    fmt(hedge.inda_open_avg), fmt(hedge.inda_close_avg), fmt(hedge.inda_pnl_usd, money=True),
                    fmt(hedge.commissions_usd, money=True), fmt(hedge.fx_rate), fmt(hedge.pnl_cny, money=True),
                    basket.hedge_status,
                ]
            )
        classic.fill_table(
            self.mapping_table,
            [
                "轮次", "赎回日", "账户", "基金份额", "NIFTY目标", "NIFTY开/平", "NIFTY开价", "NIFTY平价",
                "NIFTY盈亏USD", "INDA目标", "INDA开/平", "INDA开价", "INDA平价", "INDA盈亏USD",
                "佣金USD", "USD/CNH", "对冲盈亏RMB", "状态",
            ],
            rows,
        )

    def populate_settlements(self) -> None:
        assert self.result is not None
        rows = []
        for basket in self.result.baskets:
            value = basket.settlement
            if value is None:
                continue
            rows.append(
                [
                    basket.sequence, basket.account, basket.redeem_day.isoformat(),
                    value.expected_statement_day.isoformat(), value.expected_available_day.isoformat(),
                    fmt(value.actual_statement_day), fmt(value.actual_available_day), fmt(value.gross_amount, money=True),
                    fmt(value.fee_amount, money=True), fmt(value.net_amount, money=True), value.amount_source,
                    value.available_day_source, value.status,
                ]
            )
        classic.fill_table(
            self.settlement_table,
            [
                "轮次", "账户", "赎回日", "预计T+5交割", "预计T+6可用", "实际交割单日", "确认可用日",
                "毛额", "赎回费", "净赎回款", "金额来源", "可用日来源", "状态",
            ],
            rows,
        )

    def populate_cash(self) -> None:
        rows = [
            [
                item.redeem_day.isoformat(), item.statement_day.isoformat() if item.statement_day else "--",
                item.account, item.contract_no or "--", fmt(item.gross_amount, money=True),
                fmt(item.fee_amount, money=True), fmt(item.net_amount, money=True), item.source,
            ]
            for item in self.store.list_redemptions()
            if item.gross_amount is not None or item.fee_amount is not None or item.net_amount is not None
        ]
        classic.fill_table(
            self.cash_table,
            ["赎回日", "交割单日", "账户", "合同号", "毛额", "费用", "净款", "来源"],
            rows,
        )

    def populate_calendar(self) -> None:
        year = python_date(self.calculation_day.date()).year
        official = sorted(OFFICIAL_SZSE_HOLIDAYS_BY_YEAR.get(year, ()))
        fund_days = sorted(parse_holidays(self.raw_config.get("fund_closed_days")))
        rows = [[item.isoformat(), "深交所官方休市", "内置年度公告"] for item in official]
        rows.extend([item.isoformat(), "164824额外暂停开放", "用户配置"] for item in fund_days if item.year == year)
        classic.fill_table(self.calendar_table, ["日期", "类型", "来源"], rows)

    def populate_warnings(self) -> None:
        assert self.result is not None
        rows: list[list[object]] = []
        for warning in self.result.warnings:
            rows.append(["全局", "--", "提示", warning])
        for basket in self.result.baskets:
            for warning in basket.warnings:
                rows.append([basket.sequence, basket.redeem_day.isoformat(), basket.data_quality, warning])
        for account, item in self.result.account_snapshots.items():
            for warning in item.get("warnings", ()):
                rows.append([account, fmt(item.get("last_trade_day")), "持仓阻断", warning])
        classic.fill_table(self.warning_table, ["对象", "日期", "类型", "说明"], rows)

    def add_manual_redemption(self) -> None:
        if self.result is None:
            QMessageBox.warning(self, "当前不可登记", "请先完成计算并修复数据源错误。")
            return
        dialog = ManualRedemptionDialog(
            python_date(self.calculation_day.date()),
            self.result.account_snapshots,
            self,
        )
        if dialog.exec_() != QDialog.Accepted:
            return
        try:
            event_day = python_date(dialog.day.date())
            if event_day != python_date(self.calculation_day.date()):
                self.calculation_day.setDate(qdate_value(event_day))
                self.calculate()
            if self.result is None:
                raise ValueError("所选日期无法完成持仓计算")
            account = dialog.account.currentText()
            qty = dialog.qty.value()
            contract = dialog.contract.text().strip()
            event_id = "manual:" + hashlib.sha1(
                f"manual|{account}|{event_day}|{qty}|{contract}".encode("utf-8")
            ).hexdigest()[:20]
            known = {item.event_id: item for item in self.store.list_redemptions()}
            existing = known.get(event_id)
            available = int(self.result.account_snapshots.get(account, {}).get("eligible_qty", 0))
            allowed = available + (existing.qty if existing is not None else 0)
            if qty > allowed:
                raise ValueError(f"{account} 当前最多可登记 {allowed:,} 份，已阻止超额赎回 {qty:,} 份")
            event = RedemptionEvent(
                event_id=event_id,
                account=account,
                redeem_day=event_day,
                qty=qty,
                source="manual",
                contract_no=contract,
                net_amount=parse_optional_decimal(dialog.net.text()),
                nav_per_share=parse_optional_decimal(dialog.nav.text()),
            )
            self.store.upsert_redemption(event)
            self.calculate()
        except Exception as exc:
            QMessageBox.warning(self, "登记失败", str(exc))

    def delete_selected_redemption(self) -> None:
        row = self.redemption_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "未选择记录", "请先在赎回登记表中选择一行。")
            return
        event_item = self.redemption_table.item(row, 10)
        if event_item is None or not event_item.text().strip():
            QMessageBox.warning(self, "无法删除", "所选行缺少事件ID。")
            return
        event_id = event_item.text().strip()
        summary = " | ".join(
            self.redemption_table.item(row, column).text()
            for column in range(min(5, self.redemption_table.columnCount()))
            if self.redemption_table.item(row, column) is not None
        )
        answer = QMessageBox.question(
            self,
            "确认删除赎回登记",
            f"将删除：\n{summary}\n\n删除后会释放对应赎回预留，是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        if not self.store.delete_redemption(event_id):
            QMessageBox.warning(self, "删除失败", "主账中未找到该事件，可能已被删除。")
            return
        self.calculate()

    def import_statement(self) -> None:
        configured = str(self.raw_config.get("redemption_statement_path") or "").strip()
        path, _filter = QFileDialog.getOpenFileName(
            self,
            "选择164824赎回交割单",
            configured or str(Path.home()),
            "表格 (*.xlsx *.xls *.csv);;所有文件 (*)",
        )
        if not path:
            return
        try:
            imported = load_redemption_statement(path, self.config.fund_code)
            preview = "\n".join(
                f"{item.account} | 赎回日 {item.redeem_day} | {item.qty:,}份 | 交割单日 {fmt(item.statement_day)} | 净款 {fmt(item.net_amount, money=True)}"
                for item in imported.events[:20]
            ) or "未识别到赎回行"
            if imported.issues:
                preview += "\n\n异常：\n" + "\n".join(f"第{item.row_number}行：{item.message}" for item in imported.issues[:10])
            if not imported.events:
                QMessageBox.warning(self, "未识别到可导入记录", preview)
                return
            answer = QMessageBox.question(
                self,
                "交割单导入预览",
                preview + "\n\n确认写入本地赎回主账吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            for event in imported.events:
                self.store.upsert_redemption(event)
            self.raw_config["redemption_statement_path"] = path
            save_json_config(CONFIG_PATH, self.raw_config)
            self.calculate()
        except Exception as exc:
            QMessageBox.warning(self, "导入失败", str(exc))

    def export_csv(self) -> None:
        if self.result is None:
            return
        path, _filter = QFileDialog.getSaveFileName(
            self,
            "导出印度赎回篮子汇总",
            str(ROOT / "164824印度赎回收益汇总.csv"),
            "CSV (*.csv)",
        )
        if not path:
            return
        rows = []
        for basket in self.result.baskets:
            settlement = basket.settlement
            rows.append(
                {
                    "篮子号": basket.basket_id,
                    "账户": basket.account,
                    "赎回日": basket.redeem_day.isoformat(),
                    "份额": basket.redeem_qty,
                    "国内FIFO成本": basket.domestic_cost,
                    "净赎回款": basket.domestic_net_amount or "",
                    "国内收益": basket.domestic_pnl or "",
                    "对冲收益RMB": basket.hedge.pnl_cny,
                    "总收益RMB": basket.total_pnl_cny or "",
                    "T+5交割日": settlement.expected_statement_day if settlement else "",
                    "T+6可用日": settlement.expected_available_day if settlement else "",
                    "结算状态": basket.settlement_status,
                    "IB状态": basket.hedge_status,
                    "提示": "；".join(basket.warnings),
                }
            )
        if not rows:
            QMessageBox.information(self, "暂无数据", "当前没有可导出的赎回篮子。")
            return
        with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.status_label.setText(f"已导出：{path}")

    def pull_ib(self) -> None:
        if self._ib_thread is not None:
            return
        if str(self.raw_config.get("ib_data_source_mode") or "flex_auto") != "flex_auto":
            QMessageBox.information(self, "本地模式", "当前使用本地IB CSV，请在数据源设置中选择文件。")
            return
        self.pull_ib_button.setEnabled(False)
        self.status_label.setText("正在拉取IB Flex；当前界面不会发送任何交易订单...")
        thread = QThread(self)
        worker = classic.IbFlexRefreshWorker(self.raw_config)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self.handle_ib_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._ib_thread = thread
        self._ib_worker = worker
        thread.start()

    def handle_ib_finished(self, payload: object) -> None:
        values = dict(payload)
        self._ib_thread = None
        self._ib_worker = None
        self.pull_ib_button.setEnabled(True)
        error = str(values.get("error") or "")
        if error:
            QMessageBox.warning(self, "IB拉取失败", error)
        else:
            result = values.get("result")
            if result is not None:
                self.status_label.setText(result.status_text())
        self.calculate()

    def shutdown(self) -> None:
        worker = self._ib_worker
        if worker is not None and hasattr(worker, "cancel"):
            worker.cancel()
        thread = self._ib_thread
        if thread is not None:
            thread.quit()
            thread.wait(3000)
        self.tws_client.shutdown()


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setApplicationName("ETF 赎回收益计算器")
    classic.apply_light_theme(app)
    classic.configure_application_font(app)
    window = IndiaMainWindow()
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
