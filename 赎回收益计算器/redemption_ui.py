from __future__ import annotations

import csv
import json
import sys
import traceback
from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from PyQt5.QtCore import QDate, QFileSystemWatcher, QObject, QPoint, QPointF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QPainter, QPainterPath, QPalette, QPen
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
    QApplication,
    QCalendarWidget,
    QDialog,
    QDialogButtonBox,
    QDateEdit,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QSpinBox,
    QSplitter,
    QStyleFactory,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import redemption_engine as engine
import fx_rates
import szse_pcf


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
OVERRIDES_PATH = ROOT / "ib_mapping_overrides.json"
DEFAULT_CONFIG = {
    "qmt1_path": "/Users/ellis/Desktop/ETF交割/6.22/qmt1.xlsx",
    "qmt2_path": "",
    "ib_path": "/Users/ellis/Desktop/ETF交割/6.22/U15286908_20260601_20260629.csv",
    "fx_rate": "6.79635",
    "auto_refresh": True,
    "market_holidays": [],
    "transfer_contract_gap": 1000,
    "szse_pcf_cache_dir": str(ROOT / "szse_pcf_cache"),
    "fx_rates_csv_path": str(ROOT / "fx_data" / "fx_rates.csv"),
}


def load_config() -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                config.update(payload)
        except (OSError, json.JSONDecodeError):
            pass
    return config


def save_config(config: dict[str, object]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def fmt_money(value: Decimal) -> str:
    return f"{engine.money(value):,.2f}"


def fmt_decimal(value: Decimal, places: int = 6) -> str:
    return f"{value:,.{places}f}"


def configured_table() -> QTableWidget:
    table = QTableWidget()
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def fill_table(
    table: QTableWidget,
    headers: list[str],
    rows: list[list[object]],
    *,
    payloads: list[object] | None = None,
) -> None:
    table.setSortingEnabled(False)
    table.clear()
    table.setColumnCount(len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            if payloads is not None and column_index == 0:
                item.setData(Qt.UserRole, payloads[row_index])
            if isinstance(value, (int, float, Decimal)):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(row_index, column_index, item)
    table.resizeColumnsToContents()
    for column in range(table.columnCount()):
        if table.columnWidth(column) > 240:
            table.setColumnWidth(column, 240)


def clear_layout(layout) -> None:
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        child_layout = item.layout()
        if widget is not None:
            widget.deleteLater()
        elif child_layout is not None:
            clear_layout(child_layout)


PCF_SUMMARY_HIGHLIGHT_FIELDS = {"RedemptionLimit", "NAVperCU", "CashComponent"}
PCF_SUMMARY_HIDDEN_FIELDS = {
    "FundManagementCompany",
    "UnderlyingSecurityID",
    "Publish",
    "RecordNum",
    "TotalRecordNum",
}
PCF_COMPONENT_HIDDEN_FIELDS = {"UnderlyingSecurityID"}
PCF_FIELD_DATE_SOURCE = {
    "CashComponent": "PreTradingDay",
    "NAVperCU": "PreTradingDay",
    "NAV": "PreTradingDay",
    "RedemptionLimit": "TradingDay",
}


def normalize_business_day(value: date, *, prefer_backward: bool = True) -> date:
    current = value
    if current.weekday() < 5:
        return current
    step = -1 if prefer_backward else 1
    while current.weekday() >= 5:
        current += timedelta(days=step)
    return current


def shift_business_day(value: date, step: int) -> date:
    if step == 0:
        return value
    current = value
    remaining = abs(step)
    direction = 1 if step > 0 else -1
    while remaining:
        current += timedelta(days=direction)
        if current.weekday() < 5:
            remaining -= 1
    return current


def pcf_field_reference_day_text(metadata: dict[str, str], field: str, fallback_day: date) -> str:
    day_key = PCF_FIELD_DATE_SOURCE.get(field)
    if day_key:
        raw_day = metadata.get(day_key) or ""
        if raw_day:
            return szse_pcf.display_value("TradingDay", raw_day)
    trading_day = metadata.get("TradingDay") or fallback_day.strftime("%Y%m%d")
    return szse_pcf.display_value("TradingDay", trading_day)


class PcfLoadWorker(QObject):
    finished = pyqtSignal(object)

    def __init__(self, cache_root: Path, fx_csv_path: Path, trading_day: date, force_refresh: bool) -> None:
        super().__init__()
        self.cache_root = cache_root
        self.fx_csv_path = fx_csv_path
        self.trading_day = trading_day
        self.force_refresh = force_refresh

    def run(self) -> None:
        store = szse_pcf.SzsePcfStore(self.cache_root)
        fx_store = fx_rates.FxRateStore(self.fx_csv_path)
        result = {
            "trading_day": self.trading_day,
            "force_refresh": self.force_refresh,
            "index": None,
            "index_error": "",
            "detail": None,
            "detail_error": "",
            "fx_hours": [],
            "fx_matrix": [],
            "fx_error": "",
        }
        try:
            result["index"] = store.ensure_target_day_index(self.trading_day)
        except Exception as exc:
            result["index_error"] = str(exc)
            self.finished.emit(result)
            return

        try:
            fx_store.ensure_trade_date(self.trading_day, force_refresh=self.force_refresh)
            hours, matrix = fx_store.build_day_matrix(self.trading_day)
            result["fx_hours"] = hours
            result["fx_matrix"] = matrix
        except Exception as exc:
            result["fx_error"] = str(exc)

        try:
            result["detail"] = store.ensure_target_detail(self.trading_day, force_refresh=self.force_refresh)
        except Exception as exc:
            result["detail_error"] = str(exc)

        self.finished.emit(result)


class FilePicker(QWidget):
    def __init__(self, title: str, value: str, pattern: str) -> None:
        super().__init__()
        self.title = title
        self.pattern = pattern
        self.edit = QLineEdit(value)
        self.button = QPushButton("选择")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.button.clicked.connect(self.choose)

    def choose(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, self.title, self.edit.text(), self.pattern)
        if path:
            self.edit.setText(path)

    def value(self) -> str:
        return self.edit.text().strip()


class SettingsDialog(QDialog):
    def __init__(self, config: dict[str, object], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("数据源设置")
        self.resize(820, 220)
        self.qmt1 = FilePicker("选择 QMT1 完整交割单", str(config.get("qmt1_path") or ""), "Excel (*.xlsx *.xls)")
        self.qmt2 = FilePicker("选择 QMT2 完整交割单", str(config.get("qmt2_path") or ""), "Excel (*.xlsx *.xls)")
        self.ib = FilePicker("选择 IB 完整成交汇总", str(config.get("ib_path") or ""), "CSV (*.csv)")
        self.transfer_gap = QSpinBox()
        self.transfer_gap.setRange(0, 1_000_000)
        self.transfer_gap.setValue(int(config.get("transfer_contract_gap") or engine.DEFAULT_TRANSFER_CONTRACT_GAP))
        self.transfer_gap.setSuffix(" 号")
        self.transfer_gap.setToolTip("无成交时间时，用合同编号差作为邻近成交代理；识别同日、跨账户、方向相反、数量完全相同的组合，买卖先后均可。")
        form = QFormLayout()
        form.addRow("QMT1", self.qmt1)
        form.addRow("QMT2（可空）", self.qmt2)
        form.addRow("IB", self.ib)
        form.addRow("调仓合同号最大间隔", self.transfer_gap)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def values(self) -> dict[str, object]:
        return {
            "qmt1_path": self.qmt1.value(),
            "qmt2_path": self.qmt2.value(),
            "ib_path": self.ib.value(),
            "transfer_contract_gap": self.transfer_gap.value(),
        }


class HolidayDialog(QDialog):
    def __init__(self, values: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("休市日设置")
        self.resize(620, 460)
        self.calendar = QCalendarWidget()
        self.calendar.setGridVisible(True)
        self.list_widget = QListWidget()
        for value in sorted(set(values)):
            self.list_widget.addItem(value)
        self.add_button = QPushButton("添加所选日期")
        self.remove_button = QPushButton("删除选中日期")
        self.add_button.clicked.connect(self.add_selected_day)
        self.remove_button.clicked.connect(self.remove_selected_days)
        note = QLabel("休市日会同时影响 T+3 现金差额日和 T+6 现金替代款预计到账日；周六、周日始终自动跳过。")
        note.setWordWrap(True)
        controls = QHBoxLayout()
        controls.addWidget(self.add_button)
        controls.addWidget(self.remove_button)
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

    def add_selected_day(self) -> None:
        value = self.calendar.selectedDate().toString("yyyy-MM-dd")
        existing = {self.list_widget.item(index).text() for index in range(self.list_widget.count())}
        if value not in existing:
            self.list_widget.addItem(value)
            self.list_widget.sortItems()

    def remove_selected_days(self) -> None:
        for item in self.list_widget.selectedItems():
            self.list_widget.takeItem(self.list_widget.row(item))

    def values(self) -> list[str]:
        return [self.list_widget.item(index).text() for index in range(self.list_widget.count())]


class IbMappingDialog(QDialog):
    def __init__(
        self,
        basket: engine.BasketResult,
        trades: tuple[engine.IbTrade, ...],
        open_ids: set[str],
        close_ids: set[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"第 {basket.sequence} 篮子 IB 人工指定")
        self.resize(1180, 720)
        self.target = basket.hedge_target
        self.open_table = configured_table()
        self.close_table = configured_table()
        for table in (self.open_table, self.close_table):
            table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        sell_trades = [item for item in trades if item.qty < 0]
        buy_trades = [item for item in trades if item.qty > 0]
        self._populate(self.open_table, sell_trades, open_ids)
        self._populate(self.close_table, buy_trades, close_ids)
        note = QLabel(
            f"目标数量：{self.target:,} 股。允许选择超过目标，计算时按时间顺序只取前 {self.target:,} 股，最后一笔按比例拆分。"
        )
        note.setWordWrap(True)
        splitter = QSplitter(Qt.Horizontal)
        open_box = QGroupBox("做空开仓（SELL）")
        open_layout = QVBoxLayout(open_box)
        open_layout.addWidget(self.open_table)
        close_box = QGroupBox("回补平仓（BUY）")
        close_layout = QVBoxLayout(close_box)
        close_layout.addWidget(self.close_table)
        splitter.addWidget(open_box)
        splitter.addWidget(close_box)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.validate_and_accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(note)
        layout.addWidget(splitter, 1)
        layout.addWidget(buttons)

    def _populate(self, table: QTableWidget, trades: list[engine.IbTrade], selected_ids: set[str]) -> None:
        headers = ["时间（IB账单）", "方向", "数量", "价格", "成交额USD", "佣金USD", "标记", "交易ID"]
        rows = [
            [
                item.dt.strftime("%Y-%m-%d %H:%M:%S"),
                item.side,
                abs(item.qty),
                fmt_decimal(item.price, 4),
                fmt_money(item.gross),
                fmt_decimal(item.commission, 6),
                item.marker,
                item.id,
            ]
            for item in trades
        ]
        fill_table(table, headers, rows, payloads=[item.id for item in trades])
        for row, trade in enumerate(trades):
            if trade.id in selected_ids:
                for column in range(table.columnCount()):
                    table.item(row, column).setSelected(True)

    @staticmethod
    def selected_ids(table: QTableWidget) -> list[str]:
        rows = sorted({index.row() for index in table.selectionModel().selectedRows()})
        result: list[str] = []
        for row in rows:
            item = table.item(row, 0)
            value = item.data(Qt.UserRole) if item is not None else None
            if value:
                result.append(str(value))
        return result

    def validate_and_accept(self) -> None:
        open_qty = self._selected_qty(self.open_table)
        close_qty = self._selected_qty(self.close_table)
        if open_qty < self.target or close_qty < self.target:
            QMessageBox.warning(
                self,
                "数量不足",
                f"开仓已选 {open_qty:,} 股，平仓已选 {close_qty:,} 股，均需至少 {self.target:,} 股。",
            )
            return
        self.accept()

    @staticmethod
    def _selected_qty(table: QTableWidget) -> int:
        return sum(int(table.item(row, 2).text().replace(",", "")) for row in {x.row() for x in table.selectionModel().selectedRows()})


class BasketDetailDialog(QDialog):
    def __init__(self, basket: engine.BasketResult, parent=None) -> None:
        super().__init__(parent)
        self.basket = basket
        self.setWindowTitle(f"第 {basket.sequence} 篮子明细 - {basket.redeem_day:%Y-%m-%d}")
        self.resize(1120, 720)

        summary_box = QGroupBox("篮子概要")
        summary_grid = QGridLayout(summary_box)
        summary_grid.setContentsMargins(8, 8, 8, 8)
        summary_grid.setHorizontalSpacing(16)
        summary_rows = [
            ("状态", basket.status),
            ("赎回日期", basket.redeem_day.isoformat()),
            ("来源/合同", f"{basket.source} / {basket.contract_no}"),
            ("赎回份额", f"{basket.redeem_qty:,}"),
            ("国内成本", f"{fmt_money(basket.domestic_cost)} RMB"),
            ("退款/现金差额", f"{fmt_money(basket.refund_amount)} / {fmt_money(basket.cash_difference)} RMB"),
            ("国内盈亏", f"{fmt_money(basket.domestic_pnl)} RMB"),
            ("IB净盈亏", f"{fmt_decimal(basket.ib_pnl_usd)} USD"),
            ("全局汇率", fmt_decimal(basket.fx_rate, 6)),
            ("合计盈亏", f"{fmt_money(basket.total_pnl_cny)} RMB"),
            ("IB映射", "人工指定" if basket.manual_ib_mapping else "默认 FIFO"),
            ("IB目标", f"{basket.hedge_target:,} 股"),
        ]
        for index, (label, value) in enumerate(summary_rows):
            row = index // 4
            column = (index % 4) * 2
            key = QLabel(label)
            key.setObjectName("summaryKey")
            val = QLabel(value)
            val.setObjectName("summaryValuePositive" if label == "合计盈亏" and basket.total_pnl_cny >= 0 else "summaryValue")
            val.setTextInteractionFlags(Qt.TextSelectableByMouse)
            summary_grid.addWidget(key, row, column)
            summary_grid.addWidget(val, row, column + 1)

        self.domestic_table = configured_table()
        self.ib_open_table = configured_table()
        self.ib_close_table = configured_table()
        self._populate_domestic()
        self._populate_ib(self.ib_open_table, basket.ib_open)
        self._populate_ib(self.ib_close_table, basket.ib_close)

        tabs = QTabWidget()
        tabs.addTab(self.domestic_table, f"国内买入 FIFO ({len(basket.domestic_matches)})")
        tabs.addTab(self.ib_open_table, f"IB 做空开仓 ({sum(item.qty for item in basket.ib_open):,}股)")
        tabs.addTab(self.ib_close_table, f"IB 回补平仓 ({sum(item.qty for item in basket.ib_close):,}股)")

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(summary_box)
        layout.addWidget(tabs, 1)
        layout.addWidget(buttons)

    def _populate_domestic(self) -> None:
        rows = []
        for index, item in enumerate(self.basket.domestic_matches, start=1):
            unit_cost = item.cost / Decimal(item.qty) if item.qty else Decimal("0")
            rows.append(
                [
                    index,
                    item.source,
                    item.trade_day.isoformat(),
                    item.contract_no,
                    f"{item.qty:,}",
                    fmt_decimal(unit_cost, 6),
                    fmt_money(item.cost),
                ]
            )
        fill_table(
            self.domestic_table,
            ["序号", "来源账户", "买入日期", "买入合同", "采用数量", "单位成本", "分摊成本RMB"],
            rows or [["--", "--", "--", "--", "--", "--", "暂无国内买入匹配"]],
        )

    @staticmethod
    def _populate_ib(table: QTableWidget, slices: tuple[engine.IbSlice, ...]) -> None:
        rows = [
            [
                index,
                item.dt.strftime("%Y-%m-%d %H:%M:%S"),
                item.side,
                f"{item.qty:,}",
                fmt_decimal(item.price, 4),
                fmt_money(item.gross),
                fmt_decimal(item.commission, 6),
                item.trade_id,
            ]
            for index, item in enumerate(slices, start=1)
        ]
        fill_table(
            table,
            ["序号", "时间（IB账单）", "方向", "采用数量", "价格", "分摊成交额USD", "分摊佣金USD", "交易ID"],
            rows or [["--", "--", "--", "--", "--", "--", "--", "暂无IB匹配"]],
        )


class ConnectorOverlay(QWidget):
    def __init__(self, owner: "BasketMappingTab", parent: QWidget) -> None:
        super().__init__(parent)
        self.owner = owner
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.setAttribute(Qt.WA_NoSystemBackground)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def _row_point(self, table: QTableWidget, row: int, side: str) -> QPoint | None:
        if row < 0 or row >= table.rowCount():
            return None
        top = table.rowViewportPosition(row)
        height = table.rowHeight(row)
        if top + height < 0 or top > table.viewport().height():
            return None
        x = table.viewport().width() if side == "right" else 0
        # The overlay and tables are siblings; map through global coordinates.
        point = table.viewport().mapToGlobal(QPoint(x, top + height // 2))
        return self.mapFromGlobal(point)

    def _draw_path(self, painter: QPainter, start: QPoint, end: QPoint, color: QColor) -> None:
        pen = QPen(color)
        pen.setWidthF(1.5)
        pen.setStyle(Qt.DashLine)
        pen.setCapStyle(Qt.RoundCap)
        painter.setPen(pen)
        path = QPainterPath(QPointF(start))
        bend = max(18, abs(end.x() - start.x()) // 2)
        if start.x() <= end.x():
            path.cubicTo(
                QPointF(start.x() + bend, start.y()),
                QPointF(end.x() - bend, end.y()),
                QPointF(end),
            )
        else:
            path.cubicTo(
                QPointF(start.x() - bend, start.y()),
                QPointF(end.x() + bend, end.y()),
                QPointF(end),
            )
        painter.drawPath(path)

    def _draw_brackets(
        self,
        painter: QPainter,
        table: QTableWidget,
        groups: dict[str, list[int]],
        color: QColor,
    ) -> None:
        for rows in groups.values():
            points = [self._row_point(table, row, "right") for row in sorted(set(rows))]
            visible = [point for point in points if point is not None]
            if not visible:
                continue
            x = visible[0].x() - 8
            first_y, last_y = visible[0].y(), visible[-1].y()
            pen = QPen(color)
            pen.setWidthF(1.4)
            pen.setStyle(Qt.DashLine)
            pen.setCapStyle(Qt.RoundCap)
            painter.setPen(pen)
            painter.drawLine(x, first_y, x, last_y)
            painter.drawLine(x - 7, first_y, x, first_y)
            painter.drawLine(x - 7, last_y, x, last_y)

    def paintEvent(self, _event) -> None:
        painter = QPainter(self)
        if not painter.isActive():
            return
        painter.setRenderHint(QPainter.Antialiasing)
        center = self.owner.basket_table
        for basket_id, basket_row in self.owner.basket_rows.items():
            basket = self.owner.basket_by_id.get(basket_id)
            if basket is None:
                continue
            color = QColor(self.owner.basket_color(basket.sequence)).darker(135)
            center_left = self._row_point(center, basket_row, "left")
            center_right = self._row_point(center, basket_row, "right")
            if center_left is not None:
                rows = self.owner.domestic_basket_rows.get(basket_id, [])
                points = [self._row_point(self.owner.domestic_table, row, "right") for row in rows]
                visible = [point for point in points if point is not None]
                if visible:
                    endpoints = [visible[0]] if len(visible) == 1 else [visible[0], visible[-1]]
                    for point in endpoints:
                        self._draw_path(painter, point, center_left, color)
            if center_right is not None:
                rows = self.owner.ib_basket_rows.get(basket_id, [])
                points = [self._row_point(self.owner.ib_table, row, "left") for row in rows]
                visible = [point for point in points if point is not None]
                if visible:
                    endpoints = [visible[0]] if len(visible) == 1 else [visible[0], visible[-1]]
                    for point in endpoints:
                        self._draw_path(painter, center_right, point, color)
        self._draw_brackets(painter, self.owner.domestic_table, self.owner.domestic_special_rows, QColor("#B45309"))
        self._draw_brackets(painter, self.owner.ib_table, self.owner.ib_special_rows, QColor("#B91C1C"))
        painter.end()


class MappingLaneArea(QWidget):
    def __init__(self, owner: "BasketMappingTab") -> None:
        super().__init__()
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(owner.domestic_table)
        splitter.addWidget(owner.basket_table)
        splitter.addWidget(owner.ib_table)
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 3)
        splitter.setStretchFactor(2, 4)
        splitter.setSizes([520, 380, 520])
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(splitter)
        self.overlay = ConnectorOverlay(owner, self)
        self.overlay.raise_()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self.overlay.setGeometry(self.rect())
        self.overlay.raise_()


class BasketMappingTab(QWidget):
    BASKET_COLORS = (
        "#DCFCE7", "#DBEAFE", "#FEF3C7", "#FCE7F3",
        "#EDE9FE", "#CFFAFE", "#FFEDD5", "#E0F2FE",
    )
    SPECIAL_COLORS = {
        "venue": "#F3F4F6",
        "transfer": "#FFEDD5",
        "ib_self": "#FEE2E2",
        "unallocated": "#FFFFFF",
    }

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.result: engine.CalculationResult | None = None
        self.basket_by_id: dict[str, engine.BasketResult] = {}
        self.basket_rows: dict[str, int] = {}
        self.domestic_basket_rows: dict[str, list[int]] = defaultdict(list)
        self.ib_basket_rows: dict[str, list[int]] = defaultdict(list)
        self.domestic_special_rows: dict[str, list[int]] = defaultdict(list)
        self.ib_special_rows: dict[str, list[int]] = defaultdict(list)
        self._dates_initialized = False

        self.start_date = QDateEdit()
        self.end_date = QDateEdit()
        for editor in (self.start_date, self.end_date):
            editor.setCalendarPopup(True)
            editor.setDisplayFormat("yyyy-MM-dd")
        self.apply_button = QPushButton("应用区间")
        self.all_button = QPushButton("显示全部")
        self.legend = QLabel("篮子同色关联  ·  橙色=QMT1/2调仓  ·  灰色=国内自平  ·  红色=IB自平  ·  白色=未分配")
        self.legend.setObjectName("sourceHint")

        self.domestic_table = self._lane_table("国内 159518｜日期  Seq  账户  方向  数量  价格")
        self.basket_table = self._lane_table("赎回篮子")
        self.basket_table.setWordWrap(True)
        self.ib_table = self._lane_table("IB XOP｜日期时间  方向  数量  价格")
        self.lane_area = MappingLaneArea(self)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("显示区间"))
        controls.addWidget(self.start_date)
        controls.addWidget(QLabel("至"))
        controls.addWidget(self.end_date)
        controls.addWidget(self.apply_button)
        controls.addWidget(self.all_button)
        controls.addSpacing(12)
        controls.addWidget(self.legend, 1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(controls)
        layout.addWidget(self.lane_area, 1)

        self.apply_button.clicked.connect(self.populate)
        self.all_button.clicked.connect(self.show_all)
        for table in (self.domestic_table, self.basket_table, self.ib_table):
            table.verticalScrollBar().valueChanged.connect(self.lane_area.overlay.update)
            table.horizontalScrollBar().valueChanged.connect(self.lane_area.overlay.update)

    @staticmethod
    def _lane_table(header: str) -> QTableWidget:
        table = configured_table()
        table.setAlternatingRowColors(False)
        table.setColumnCount(1)
        table.setHorizontalHeaderLabels([header])
        table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        table.setShowGrid(False)
        return table

    def basket_color(self, sequence: int) -> str:
        return self.BASKET_COLORS[(sequence - 1) % len(self.BASKET_COLORS)]

    @staticmethod
    def _qdate(value: date) -> QDate:
        return QDate(value.year, value.month, value.day)

    @staticmethod
    def _python_date(value: QDate) -> date:
        return date(value.year(), value.month(), value.day())

    def update_data(self, result: engine.CalculationResult) -> None:
        self.result = result
        self.basket_by_id = {item.id: item for item in result.baskets}
        days = [item.trade_day for item in result.qmt_records]
        days.extend(item.redeem_day for item in result.baskets)
        days.extend(item.dt.date() for item in result.ib_trades)
        if days:
            minimum, maximum = min(days), max(days)
            for editor in (self.start_date, self.end_date):
                editor.setMinimumDate(self._qdate(minimum))
                editor.setMaximumDate(self._qdate(maximum))
            if not self._dates_initialized:
                self.start_date.setDate(self._qdate(minimum))
                self.end_date.setDate(self._qdate(maximum))
                self._dates_initialized = True
        self.populate()

    def show_all(self) -> None:
        if self.result is None:
            return
        days = [item.trade_day for item in self.result.qmt_records]
        days.extend(item.redeem_day for item in self.result.baskets)
        days.extend(item.dt.date() for item in self.result.ib_trades)
        if days:
            self.start_date.setDate(self._qdate(min(days)))
            self.end_date.setDate(self._qdate(max(days)))
        self.populate()

    @staticmethod
    def _record_key(source: str, trade_day: date, contract_no: int) -> tuple[str, date, int]:
        return source, trade_day, contract_no

    @staticmethod
    def _display_price(record: engine.QmtRecord) -> Decimal:
        if record.price:
            return record.price
        return abs(record.amount) / Decimal(record.qty) if record.qty else Decimal("0")

    def _domestic_rows(self, start: date, end: date) -> list[dict[str, object]]:
        assert self.result is not None
        trades = [
            item for item in self.result.qmt_records
            if item.action in {"证券买入", "证券卖出"} and item.qty > 0
        ]
        seq_by_key: dict[tuple[str, date, int], int] = {}
        day_counts: dict[date, int] = defaultdict(int)
        for record in trades:
            day_counts[record.trade_day] += 1
            seq_by_key[self._record_key(record.source, record.trade_day, record.contract_no)] = day_counts[record.trade_day]

        allocations: dict[tuple[str, date, int], list[tuple[str, str, int, str]]] = defaultdict(list)
        for basket in self.result.baskets:
            for match in basket.domestic_matches:
                key = self._record_key(match.source, match.trade_day, match.contract_no)
                allocations[key].append(("basket", basket.id, match.qty, f"篮子{basket.sequence}"))
        for index, close in enumerate(self.result.venue_closes, start=1):
            group = f"venue-{index}"
            for match in close.matches:
                key = self._record_key(match.source, match.trade_day, match.contract_no)
                allocations[key].append(("venue", group, match.qty, "国内自买自平"))
        for index, transfer in enumerate(self.result.account_transfers, start=1):
            group = f"transfer-{index}"
            for match in transfer.matches:
                key = self._record_key(match.source, match.trade_day, match.contract_no)
                allocations[key].append(("transfer", group, match.qty, "QMT1/2调仓"))
        venue_sells = {
            self._record_key(item.source, item.trade_day, item.contract_no): f"venue-{index}"
            for index, item in enumerate(self.result.venue_closes, start=1)
        }
        transfer_sells = {
            self._record_key(item.sell_source, item.trade_day, item.sell_contract_no): f"transfer-{index}"
            for index, item in enumerate(self.result.account_transfers, start=1)
        }
        transfer_buys = {
            self._record_key(item.buy_source, item.trade_day, item.buy_contract_no): f"transfer-{index}"
            for index, item in enumerate(self.result.account_transfers, start=1)
        }

        rows: list[dict[str, object]] = []
        suborder = 0
        for record in trades:
            if not start <= record.trade_day <= end:
                continue
            key = self._record_key(record.source, record.trade_day, record.contract_no)
            seq = seq_by_key[key]
            price = self._display_price(record)
            base = f"{record.trade_day:%m-%d}  #{seq:03d}  {record.source}"
            if record.action == "证券买入":
                used = 0
                for kind, group, qty, label in allocations.get(key, []):
                    used += qty
                    suborder += 1
                    transfer_note = "  [QMT1/2 调仓买入腿]" if key in transfer_buys else ""
                    rows.append({
                        "sort": (record.trade_day, record.contract_no, suborder),
                        "text": f"{base}  买入  {qty:,}  @{price:.4f}  → {label}{transfer_note}",
                        "kind": kind, "group": group, "special_group": transfer_buys.get(key, ""),
                    })
                remaining = max(0, record.qty - used)
                if remaining:
                    suborder += 1
                    transfer_note = "  [QMT1/2 调仓买入腿]" if key in transfer_buys else ""
                    rows.append({
                        "sort": (record.trade_day, record.contract_no, suborder),
                        "text": f"{base}  买入  {remaining:,}  @{price:.4f}  [未分配库存]{transfer_note}",
                        "kind": "unallocated", "group": "", "special_group": transfer_buys.get(key, ""),
                    })
                continue
            suborder += 1
            kind = "transfer" if key in transfer_sells else "venue"
            group = transfer_sells.get(key) or venue_sells.get(key, "")
            label = "QMT1/2 调仓卖出腿" if kind == "transfer" else "国内自买自平"
            rows.append({
                "sort": (record.trade_day, record.contract_no, suborder),
                "text": f"{base}  卖出  {record.qty:,}  @{price:.4f}  [{label}]",
                "kind": kind, "group": group, "special_group": "",
            })
        return sorted(rows, key=lambda item: item["sort"])

    def _ib_rows(self, start: date, end: date) -> list[dict[str, object]]:
        assert self.result is not None
        allocated: dict[str, int] = defaultdict(int)
        rows: list[dict[str, object]] = []
        order = 0
        for basket in self.result.baskets:
            for item in (*basket.ib_open, *basket.ib_close):
                allocated[item.trade_id] += item.qty
                if start <= item.dt.date() <= end:
                    order += 1
                    rows.append({
                        "sort": (item.dt, order),
                        "text": f"{item.dt:%m-%d %H:%M:%S}  {item.side:<4}  {item.qty:,}  @{item.price:.4f}  → 篮子{basket.sequence}",
                        "kind": "basket", "group": basket.id,
                    })

        residual = []
        for trade in self.result.ib_trades:
            qty = max(0, abs(trade.qty) - allocated.get(trade.id, 0))
            if qty:
                residual.append([trade, qty])
        open_lots: list[list[object]] = []
        self_index = 0
        for trade, quantity in residual:
            side = trade.side
            remaining = int(quantity)
            while remaining and open_lots and open_lots[0][0].side != side:
                opening, opening_qty = open_lots[0]
                used = min(remaining, int(opening_qty))
                self_index += 1
                group = f"ib-self-{self_index}"
                label = "IB自卖自平" if opening.side == "SELL" else "IB自买自平"
                for current, role in ((opening, "开"), (trade, "平")):
                    if start <= current.dt.date() <= end:
                        order += 1
                        rows.append({
                            "sort": (current.dt, order),
                            "text": f"{current.dt:%m-%d %H:%M:%S}  {current.side:<4}  {used:,}  @{current.price:.4f}  [{label}{role}]",
                            "kind": "ib_self", "group": group,
                        })
                remaining -= used
                opening_qty = int(opening_qty) - used
                if opening_qty:
                    open_lots[0][1] = opening_qty
                else:
                    open_lots.pop(0)
            if remaining:
                open_lots.append([trade, remaining])
        for trade, quantity in open_lots:
            if start <= trade.dt.date() <= end:
                order += 1
                rows.append({
                    "sort": (trade.dt, order),
                    "text": f"{trade.dt:%m-%d %H:%M:%S}  {trade.side:<4}  {int(quantity):,}  @{trade.price:.4f}  [未分配]",
                    "kind": "unallocated", "group": "",
                })
        return sorted(rows, key=lambda item: item["sort"])

    def _fill_lane(self, table: QTableWidget, rows: list[dict[str, object]], lane: str) -> None:
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            item = QTableWidgetItem(str(row["text"]))
            kind = str(row["kind"])
            group = str(row["group"])
            if kind == "basket":
                basket = self.basket_by_id[group]
                color = self.basket_color(basket.sequence)
                target = self.domestic_basket_rows if lane == "domestic" else self.ib_basket_rows
                target[group].append(row_index)
            else:
                color = self.SPECIAL_COLORS.get(kind, "#FFFFFF")
                if group:
                    target = self.domestic_special_rows if lane == "domestic" else self.ib_special_rows
                    target[group].append(row_index)
            special_group = str(row.get("special_group") or "")
            if special_group:
                target = self.domestic_special_rows if lane == "domestic" else self.ib_special_rows
                target[special_group].append(row_index)
            item.setBackground(QColor(color))
            item.setToolTip(str(row["text"]))
            table.setItem(row_index, 0, item)
            table.setRowHeight(row_index, 29)

    def populate(self) -> None:
        if self.result is None:
            return
        start = self._python_date(self.start_date.date())
        end = self._python_date(self.end_date.date())
        if start > end:
            start, end = end, start
        self.basket_rows.clear()
        self.domestic_basket_rows.clear()
        self.ib_basket_rows.clear()
        self.domestic_special_rows.clear()
        self.ib_special_rows.clear()
        self._fill_lane(self.domestic_table, self._domestic_rows(start, end), "domestic")
        self._fill_lane(self.ib_table, self._ib_rows(start, end), "ib")

        baskets = [item for item in self.result.baskets if start <= item.redeem_day <= end]
        self.basket_table.setRowCount(max(0, len(baskets) * 2 - 1))
        for index, basket in enumerate(baskets):
            row = index * 2
            self.basket_rows[basket.id] = row
            open_qty = sum(item.qty for item in basket.ib_open)
            close_qty = sum(item.qty for item in basket.ib_close)
            mapping = "人工" if basket.manual_ib_mapping else "FIFO"
            text = (
                f"轮次 {basket.sequence}  |  {basket.status}\n"
                f"赎回  {basket.redeem_day:%Y-%m-%d}  |  {basket.source}\n"
                f"份额  {basket.redeem_qty:,}  |  合同 {basket.contract_no}\n"
                f"国内  成本 {fmt_money(basket.domestic_cost)}  |  盈亏 {fmt_money(basket.domestic_pnl)}\n"
                f"回款  退款 {fmt_money(basket.refund_amount)}  |  现金差额 {fmt_money(basket.cash_difference)}\n"
                f"IB  开仓 {open_qty:,}  |  平仓 {close_qty:,}  |  {mapping}\n"
                f"合计  {fmt_money(basket.total_pnl_cny)} RMB"
            )
            item = QTableWidgetItem(text)
            item.setBackground(QColor(self.basket_color(basket.sequence)))
            item.setTextAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            item.setToolTip("；".join(basket.warnings) if basket.warnings else text)
            self.basket_table.setItem(row, 0, item)
            self.basket_table.setRowHeight(row, 150)
            if row + 1 < self.basket_table.rowCount():
                spacer = QTableWidgetItem("")
                spacer.setFlags(Qt.NoItemFlags)
                spacer.setBackground(QColor("#FFFFFF"))
                self.basket_table.setItem(row + 1, 0, spacer)
                self.basket_table.setRowHeight(row + 1, 34)
        QTimer.singleShot(0, self.lane_area.overlay.update)


class SzsePcfTab(QWidget):
    def __init__(self, cache_root: Path, fx_csv_path: Path, parent=None) -> None:
        super().__init__(parent)
        self.target_code = szse_pcf.TARGET_FUND_CODE
        self.store = szse_pcf.SzsePcfStore(cache_root)
        self.fx_store = fx_rates.FxRateStore(fx_csv_path)
        self.cache_root = cache_root
        self.fx_csv_path = fx_csv_path
        self.current_index: szse_pcf.PcfDayIndex | None = None
        self.current_detail: szse_pcf.PcfDetail | None = None
        self._loaded_once = False
        self._worker_thread: QThread | None = None
        self._worker: PcfLoadWorker | None = None
        self._active_request: tuple[date, bool] | None = None
        self._pending_request: tuple[date, bool] | None = None

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(self._qdate(normalize_business_day(date.today(), prefer_backward=True)))
        self.prev_day_button = QPushButton("前一日")
        self.today_button = QPushButton("今日")
        self.next_day_button = QPushButton("下一日")
        self.refresh_button = QPushButton("强制刷新")
        self.status_label = QLabel(
            f"仅维护 {self.target_code}；进入该标签页后会自动在后台读取当前所选日期。"
        )
        self.status_label.setObjectName("sourceHint")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.list_table = configured_table()

        self.summary_group = QGroupBox(f"{self.target_code} 清单概要")
        self.summary_grid = QGridLayout(self.summary_group)
        self.summary_grid.setContentsMargins(8, 8, 8, 8)
        self.summary_grid.setHorizontalSpacing(12)

        self.fx_group = QGroupBox("当日汇率")
        self.fx_panel = QWidget()
        self.fx_grid = QGridLayout(self.fx_panel)
        self.fx_grid.setContentsMargins(0, 0, 0, 0)
        self.fx_grid.setHorizontalSpacing(8)
        self.fx_grid.setVerticalSpacing(8)
        fx_layout = QVBoxLayout(self.fx_group)
        fx_layout.setContentsMargins(8, 8, 8, 8)
        fx_layout.addWidget(self.fx_panel)

        self.component_table = configured_table()
        self.raw_text = QPlainTextEdit()
        self.raw_text.setReadOnly(True)
        self.raw_text.setLineWrapMode(QPlainTextEdit.NoWrap)

        structured = QWidget()
        structured_layout = QVBoxLayout(structured)
        structured_layout.setContentsMargins(8, 8, 8, 8)
        structured_layout.addWidget(self.fx_group)
        structured_layout.addWidget(self.summary_group)
        structured_layout.addWidget(self.component_table, 1)

        detail_tabs = QTabWidget()
        detail_tabs.addTab(structured, "结构化清单")
        detail_tabs.addTab(self.raw_text, "原始TXT")

        splitter = QSplitter(Qt.Horizontal)
        left_box = QGroupBox(f"{self.target_code} 当日清单")
        left_layout = QVBoxLayout(left_box)
        left_layout.addWidget(self.list_table)
        splitter.addWidget(left_box)
        splitter.addWidget(detail_tabs)
        left_box.setMaximumWidth(320)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 7)
        splitter.setSizes([260, 1140])

        controls = QHBoxLayout()
        controls.addWidget(QLabel("交易日"))
        controls.addWidget(self.date_edit)
        controls.addWidget(self.prev_day_button)
        controls.addWidget(self.today_button)
        controls.addWidget(self.next_day_button)
        controls.addWidget(self.refresh_button)
        controls.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addLayout(controls)
        layout.addWidget(splitter, 1)
        layout.addWidget(self.status_label)

        self.prev_day_button.clicked.connect(lambda: self.shift_day(-1))
        self.today_button.clicked.connect(self.jump_today)
        self.next_day_button.clicked.connect(lambda: self.shift_day(1))
        self.refresh_button.clicked.connect(self.force_refresh)
        self.date_edit.dateChanged.connect(self.handle_date_changed)
        self.show_fx_placeholder("请选择日期后加载汇率")
        self.clear_detail()

    @staticmethod
    def _qdate(value: date) -> QDate:
        return QDate(value.year, value.month, value.day)

    @staticmethod
    def _python_date(value: QDate) -> date:
        return date(value.year(), value.month(), value.day())

    def suggest_date(self, value: date | None) -> None:
        if value is None or self._loaded_once:
            return
        self.date_edit.setDate(self._qdate(normalize_business_day(value, prefer_backward=True)))

    def ensure_loaded(self) -> None:
        current_day = self._python_date(self.date_edit.date())
        if not self._loaded_once or self.current_index is None or self.current_index.trade_date != current_day:
            self.load_day()

    def selected_code(self) -> str | None:
        row = self.list_table.currentRow()
        if row < 0:
            return None
        item = self.list_table.item(row, 0)
        value = item.data(Qt.UserRole) if item is not None else None
        return str(value) if value else None

    def handle_date_changed(self, _value: QDate) -> None:
        if self._loaded_once or self._worker_thread is not None:
            self.load_day()

    def shift_day(self, days: int) -> None:
        current = self._python_date(self.date_edit.date())
        self.date_edit.setDate(self._qdate(shift_business_day(current, days)))

    def jump_today(self) -> None:
        today = normalize_business_day(date.today(), prefer_backward=True)
        q_today = self._qdate(today)
        if self.date_edit.date() == q_today:
            self.load_day()
            return
        self.date_edit.setDate(q_today)

    def force_refresh(self) -> None:
        self.load_day(force_refresh=True)

    def load_day(self, force_refresh: bool = False) -> None:
        trading_day = self._python_date(self.date_edit.date())
        request = (trading_day, force_refresh)
        if request == self._active_request or request == self._pending_request:
            return
        if self._worker_thread is not None:
            self._pending_request = request
            self.status_label.setText(
                f"{trading_day:%Y-%m-%d} 已加入读取队列，当前请求完成后继续..."
                + ("（强制刷新）" if force_refresh else "")
            )
            return
        self._start_load(request)

    def _start_load(self, request: tuple[date, bool]) -> None:
        trading_day, force_refresh = request
        self._active_request = request
        self.status_label.setText(
            f"正在后台读取 {trading_day:%Y-%m-%d} 的 {self.target_code} 申购赎回清单..."
            + ("（强制刷新）" if force_refresh else "")
        )
        thread = QThread(self)
        worker = PcfLoadWorker(self.cache_root, self.fx_csv_path, trading_day, force_refresh)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._handle_load_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._worker_thread = thread
        self._worker = worker
        thread.start()

    def _handle_load_finished(self, result: object) -> None:
        payload = dict(result)
        request = (payload["trading_day"], bool(payload["force_refresh"]))
        stale = self._pending_request is not None and self._pending_request != request
        self._active_request = None
        self._worker = None
        self._worker_thread = None
        if not stale:
            self._apply_load_result(payload)
        next_request = self._pending_request
        self._pending_request = None
        if next_request is not None:
            self._start_load(next_request)

    def _apply_load_result(self, payload: dict[str, object]) -> None:
        trading_day = payload["trading_day"]
        index_error = str(payload.get("index_error") or "")
        if index_error:
            self.current_index = None
            self.clear_detail("清单索引读取失败")
            self.show_fx_placeholder("当前日期暂无汇率数据")
            self.status_label.setText(f"{trading_day:%Y-%m-%d} 清单索引读取失败：{index_error}")
            return

        self._loaded_once = True
        self.current_index = payload["index"]
        self.populate_index(self.target_code)

        fx_error = str(payload.get("fx_error") or "")
        if fx_error:
            self.show_fx_placeholder("汇率抓取失败，当前未展示汇率数据")
        else:
            self.populate_fx_data(
                list(payload.get("fx_hours") or []),
                list(payload.get("fx_matrix") or []),
            )

        detail = payload.get("detail")
        detail_error = str(payload.get("detail_error") or "")
        if detail is None:
            self.clear_detail(
                f"{trading_day:%Y-%m-%d} 暂无 {self.target_code} 清单"
                if detail_error
                else f"{trading_day:%Y-%m-%d} 暂无 {self.target_code} 清单"
            )
            if detail_error:
                self.status_label.setText(
                    f"{trading_day:%Y-%m-%d} 暂无 {self.target_code} 申购赎回清单"
                    if "暂无" in detail_error
                    else f"{trading_day:%Y-%m-%d} 清单读取失败：{detail_error}"
                )
            else:
                self.status_label.setText(f"{trading_day:%Y-%m-%d} 暂无 {self.target_code} 申购赎回清单")
            return

        assert isinstance(detail, szse_pcf.PcfDetail)
        self.current_detail = detail
        self.populate_detail(detail)
        self.status_label.setText(
            f"已加载 {detail.item.trade_date:%Y-%m-%d} 的 {detail.item.fund_code}；成分 {len(detail.components)} 条"
        )

    def show_fx_placeholder(self, message: str) -> None:
        clear_layout(self.fx_grid)
        hint = QLabel(message)
        hint.setObjectName("sourceHint")
        hint.setWordWrap(True)
        self.fx_grid.addWidget(hint, 0, 0)

    def _create_metric_card(self, title: str, value: str, *, accent: bool = False) -> QWidget:
        card = QWidget()
        card.setObjectName("fxMetricCardAccent" if accent else "fxMetricCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        key = QLabel(title)
        key.setObjectName("fxMetricKeyAccent" if accent else "fxMetricKey")
        value_label = QLabel(value or "--")
        value_label.setObjectName("fxMetricValueAccent" if accent else "fxMetricValue")
        value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(key)
        layout.addWidget(value_label)
        return card

    def populate_fx_data(self, hours: list[str], matrix: list[dict[str, str]]) -> None:
        clear_layout(self.fx_grid)
        if not matrix:
            self.show_fx_placeholder("当前日期暂无汇率数据")
            return
        row = matrix[0]
        close_title = "收盘"
        if row.get("close_time"):
            close_title = f"收盘 {row['close_time']}"
        metrics = [("货币对", row.get("pair", "--"), False), ("SAFE中间价", row.get("safe_rate", "--"), True)]
        metrics.extend((hour, row.get(hour, "--"), False) for hour in hours)
        metrics.append((close_title, row.get("close_rate", "--"), True))
        cards_per_row = 5
        for index, (title, value, accent) in enumerate(metrics):
            widget = self._create_metric_card(title, value or "--", accent=accent)
            self.fx_grid.addWidget(widget, index // cards_per_row, index % cards_per_row)

    def populate_index(self, selected_code: str | None = None) -> None:
        if self.current_index is None:
            fill_table(self.list_table, ["代码", "名称"], [])
            self.clear_detail()
            return
        rows: list[list[object]] = []
        payloads: list[object] = []
        for item in self.current_index.items:
            rows.append([item.fund_code, item.page_label or item.title])
            payloads.append(item.fund_code)
        self.list_table.blockSignals(True)
        fill_table(self.list_table, ["代码", "名称"], rows, payloads=payloads)
        if self.list_table.columnCount() >= 1:
            self.list_table.setColumnWidth(0, 84)
        if not rows:
            self.list_table.blockSignals(False)
            self.clear_detail()
            return
        chosen = 0
        for row, payload in enumerate(payloads):
            if payload == selected_code:
                chosen = row
                break
        self.list_table.selectRow(chosen)
        self.list_table.blockSignals(False)

    def clear_detail(self, message: str | None = None) -> None:
        clear_layout(self.summary_grid)
        text = message or f"请选择日期后读取 {self.target_code} 清单"
        fill_table(self.component_table, ["提示"], [[text]])
        self.raw_text.setPlainText(f"{text}。")
        self.current_detail = None

    def _create_summary_field(self, label: str, value: str, *, accent: bool = False) -> QWidget:
        field = QWidget()
        field.setObjectName("summaryFieldAccent" if accent else "summaryField")
        layout = QVBoxLayout(field)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(3)
        key = QLabel(label)
        key.setObjectName("summaryKeyAccent" if accent else "summaryKey")
        val = QLabel(value)
        val.setObjectName("summaryValueAccent" if accent else "summaryValue")
        val.setTextInteractionFlags(Qt.TextSelectableByMouse)
        val.setWordWrap(True)
        layout.addWidget(key)
        layout.addWidget(val)
        return field

    def populate_detail(self, detail: szse_pcf.PcfDetail) -> None:
        clear_layout(self.summary_grid)

        fields: list[tuple[str, str, bool]] = []
        for field in szse_pcf.SUMMARY_FIELD_ORDER:
            if field in PCF_SUMMARY_HIDDEN_FIELDS:
                continue
            value = detail.metadata.get(field) or ""
            if value:
                label = szse_pcf.display_summary_label(field)
                reference_day_text = pcf_field_reference_day_text(detail.metadata, field, detail.item.trade_date)
                if field in PCF_SUMMARY_HIGHLIGHT_FIELDS or field == "NAV":
                    label = f"{label} · {reference_day_text}"
                fields.append(
                    (
                        label,
                        szse_pcf.display_value(field, value),
                        field in PCF_SUMMARY_HIGHLIGHT_FIELDS,
                    )
                )
        cards_per_row = 4
        for index, (label, value, accent) in enumerate(fields):
            self.summary_grid.addWidget(
                self._create_summary_field(label, value, accent=accent),
                index // cards_per_row,
                index % cards_per_row,
            )

        if detail.components:
            columns = [
                column
                for column in szse_pcf.component_columns(detail.components)
                if column not in PCF_COMPONENT_HIDDEN_FIELDS
            ]
            headers = [szse_pcf.display_component_label(column) for column in columns]
            rows = [
                [szse_pcf.display_value(column, component.get(column, "")) for column in columns]
                for component in detail.components
            ]
            fill_table(self.component_table, headers, rows)
        else:
            fill_table(self.component_table, ["提示"], [["当前条目暂无结构化 XML 成分数据"]])
        self.raw_text.setPlainText(detail.raw_text or "当前条目暂无原始 TXT 内容。")


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ETF 赎回收益计算器")
        self.resize(1420, 900)
        self.config = load_config()
        self.overrides = engine.load_overrides(OVERRIDES_PATH)
        self.result: engine.CalculationResult | None = None
        self.basket_by_id: dict[str, engine.BasketResult] = {}
        self.refreshing = False

        self.fx_spin = QDoubleSpinBox()
        self.fx_spin.setDecimals(6)
        self.fx_spin.setRange(0.000001, 1000)
        self.fx_spin.setValue(float(self.config.get("fx_rate") or 0))
        self.fx_spin.setSingleStep(0.0001)
        self.refresh_button = QPushButton("重新计算")
        self.settings_button = QPushButton("数据源")
        self.holiday_button = QPushButton("休市日")
        self.detail_button = QPushButton("显示详细")
        self.detail_button.setCheckable(True)
        self.detail_button.setChecked(False)
        self.export_button = QPushButton("导出CSV")
        self.source_label = QLabel()
        self.source_label.setObjectName("sourceHint")
        self.source_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.status_label = QLabel("等待读取")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)

        self.summary_group = QGroupBox("概览")
        self.summary_grid = QGridLayout(self.summary_group)
        self.summary_grid.setContentsMargins(6, 6, 6, 6)
        self.summary_grid.setHorizontalSpacing(6)

        self.basket_table = configured_table()
        self.basket_table.itemSelectionChanged.connect(self.refresh_details)
        self.basket_table.cellDoubleClicked.connect(self.open_basket_detail)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        overview_layout.setContentsMargins(8, 8, 8, 8)
        overview_layout.addWidget(self.summary_group)
        overview_layout.addWidget(self.basket_table, 1)

        self.domestic_matches_table = configured_table()
        self.venue_closes_table = configured_table()
        domestic_splitter = QSplitter(Qt.Vertical)
        basket_cost_box = QGroupBox("所选篮子国内 FIFO 成本")
        basket_cost_layout = QVBoxLayout(basket_cost_box)
        basket_cost_layout.addWidget(self.domestic_matches_table)
        venue_box = QGroupBox("全部场内卖出剥离（不计入赎回篮子）")
        venue_layout = QVBoxLayout(venue_box)
        venue_layout.addWidget(self.venue_closes_table)
        domestic_splitter.addWidget(basket_cost_box)
        domestic_splitter.addWidget(venue_box)

        self.ib_open_table = configured_table()
        self.ib_close_table = configured_table()
        self.mapping_button = QPushButton("人工指定所选篮子 IB 成交")
        self.clear_mapping_button = QPushButton("恢复默认 FIFO")
        ib_controls = QHBoxLayout()
        ib_controls.addWidget(self.mapping_button)
        ib_controls.addWidget(self.clear_mapping_button)
        ib_controls.addStretch(1)
        ib_splitter = QSplitter(Qt.Vertical)
        ib_open_box = QGroupBox("所选篮子做空开仓")
        ib_open_layout = QVBoxLayout(ib_open_box)
        ib_open_layout.addWidget(self.ib_open_table)
        ib_close_box = QGroupBox("所选篮子回补平仓")
        ib_close_layout = QVBoxLayout(ib_close_box)
        ib_close_layout.addWidget(self.ib_close_table)
        ib_splitter.addWidget(ib_open_box)
        ib_splitter.addWidget(ib_close_box)
        ib_widget = QWidget()
        ib_layout = QVBoxLayout(ib_widget)
        ib_layout.addLayout(ib_controls)
        ib_layout.addWidget(ib_splitter, 1)

        self.cash_table = configured_table()
        self.transfer_table = configured_table()
        self.warning_table = configured_table()
        self.mapping_tab = BasketMappingTab()
        self.pcf_tab = SzsePcfTab(
            Path(str(self.config.get("szse_pcf_cache_dir") or (ROOT / "szse_pcf_cache"))),
            Path(str(self.config.get("fx_rates_csv_path") or (ROOT / "fx_data" / "fx_rates.csv"))),
        )
        tabs = QTabWidget()
        tabs.addTab(overview, "篮子汇总")
        tabs.addTab(self.mapping_tab, "篮子配对图")
        tabs.addTab(self.pcf_tab, "申购赎回清单")
        tabs.addTab(domestic_splitter, "国内 FIFO")
        tabs.addTab(ib_widget, "IB 对冲")
        tabs.addTab(self.cash_table, "资金流水")
        tabs.addTab(self.transfer_table, "跨账户调仓")
        tabs.addTab(self.warning_table, "异常与未匹配")
        self.tabs = tabs

        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.addWidget(self.header())
        root_layout.addWidget(self.summary_group)
        root_layout.addWidget(tabs, 1)
        root_layout.addWidget(self.status_label)
        # The overview owns the same summary group; keep it only in the root header area.
        overview_layout.removeWidget(self.summary_group)
        self.setCentralWidget(root)

        self.watcher = QFileSystemWatcher(self)
        self.watcher.fileChanged.connect(self.schedule_refresh)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.setSingleShot(True)
        self.refresh_timer.setInterval(700)
        self.refresh_timer.timeout.connect(self.calculate)

        self.refresh_button.clicked.connect(self.calculate)
        self.settings_button.clicked.connect(self.open_settings)
        self.holiday_button.clicked.connect(self.open_holidays)
        self.detail_button.toggled.connect(self.toggle_details)
        self.export_button.clicked.connect(self.export_csv)
        self.fx_spin.editingFinished.connect(self.change_fx)
        self.mapping_button.clicked.connect(self.edit_ib_mapping)
        self.clear_mapping_button.clicked.connect(self.clear_ib_mapping)
        self.tabs.currentChanged.connect(self.handle_tab_changed)
        self.setup_menu()
        self.update_source_label()
        self.restart_watcher()
        QTimer.singleShot(0, self.calculate)

    def header(self) -> QWidget:
        bar = QWidget()
        layout = QHBoxLayout(bar)
        layout.setContentsMargins(8, 6, 8, 6)
        layout.addWidget(QLabel("全局 USD/CNH"))
        layout.addWidget(self.fx_spin)
        layout.addWidget(self.source_label, 1)
        layout.addWidget(self.settings_button)
        layout.addWidget(self.holiday_button)
        layout.addWidget(self.detail_button)
        layout.addWidget(self.export_button)
        layout.addWidget(self.refresh_button)
        return bar

    def setup_menu(self) -> None:
        menu = self.menuBar().addMenu("设置")
        action = QAction("数据源设置", self)
        action.triggered.connect(self.open_settings)
        menu.addAction(action)
        holiday_action = QAction("休市日设置", self)
        holiday_action.triggered.connect(self.open_holidays)
        menu.addAction(holiday_action)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        self.config.update(dialog.values())
        save_config(self.config)
        self.update_source_label()
        self.restart_watcher()
        self.calculate()

    def change_fx(self) -> None:
        self.config["fx_rate"] = f"{self.fx_spin.value():.6f}"
        save_config(self.config)
        self.calculate()

    def toggle_details(self, checked: bool) -> None:
        self.detail_button.setText("隐藏详细" if checked else "显示详细")
        selected_id = self.selected_basket_id()
        if self.result is not None:
            self.populate_baskets(selected_id)

    def open_holidays(self) -> None:
        values = self.config.get("market_holidays")
        holidays = [str(item) for item in values] if isinstance(values, list) else []
        dialog = HolidayDialog(holidays, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        self.config["market_holidays"] = dialog.values()
        save_config(self.config)
        self.calculate()

    def market_holidays(self) -> tuple[date, ...]:
        values = self.config.get("market_holidays")
        if not isinstance(values, list):
            return ()
        result: list[date] = []
        for value in values:
            try:
                result.append(date.fromisoformat(str(value)))
            except ValueError:
                continue
        return tuple(sorted(set(result)))

    def input_paths(self) -> tuple[dict[str, str | None], str]:
        qmt2 = str(self.config.get("qmt2_path") or "").strip()
        return (
            {
                "QMT1": str(self.config.get("qmt1_path") or "").strip(),
                "QMT2": qmt2 or None,
            },
            str(self.config.get("ib_path") or "").strip(),
        )

    def update_source_label(self) -> None:
        qmt, ib = self.input_paths()
        qmt2 = Path(qmt["QMT2"]).name if qmt["QMT2"] else "未配置"
        self.source_label.setText(f"QMT1 {Path(qmt['QMT1']).name if qmt['QMT1'] else '--'} | QMT2 {qmt2} | IB {Path(ib).name if ib else '--'}")

    def restart_watcher(self) -> None:
        current = self.watcher.files()
        if current:
            self.watcher.removePaths(current)
        if not bool(self.config.get("auto_refresh", True)):
            return
        qmt, ib = self.input_paths()
        paths = [str(Path(item).expanduser().resolve()) for item in [qmt.get("QMT1"), qmt.get("QMT2"), ib] if item]
        existing = [item for item in paths if Path(item).exists()]
        if existing:
            self.watcher.addPaths(existing)

    def schedule_refresh(self, _path: str) -> None:
        self.refresh_timer.start()

    def calculate(self) -> None:
        if self.refreshing:
            return
        self.refreshing = True
        selected_id = self.selected_basket_id()
        self.status_label.setText("正在读取完整交割单并计算...")
        QApplication.processEvents()
        try:
            qmt_paths, ib_path = self.input_paths()
            if not qmt_paths["QMT1"] or not ib_path:
                raise ValueError("请先配置 QMT1 和 IB 文件")
            result = engine.calculate(
                qmt_paths,
                ib_path,
                Decimal(str(self.fx_spin.value())),
                self.overrides,
                self.market_holidays(),
                int(self.config.get("transfer_contract_gap") or engine.DEFAULT_TRANSFER_CONTRACT_GAP),
            )
        except Exception as exc:
            self.status_label.setText(f"计算失败：{exc}")
            QMessageBox.critical(self, "计算失败", f"{exc}\n\n{traceback.format_exc()}")
            self.refreshing = False
            self.restart_watcher()
            return
        self.result = result
        self.basket_by_id = {item.id: item for item in result.baskets}
        self.populate_all(selected_id)
        warning_text = " | ".join(result.warnings)
        self.status_label.setText(
            f"已读取 {len(result.baskets)} 个篮子；已结算 {len(result.settled_baskets)} 个；"
            f"已结算合计 {fmt_money(result.settled_total_cny)} RMB"
            + (f" | {warning_text}" if warning_text else "")
        )
        self.refreshing = False
        self.restart_watcher()

    def handle_tab_changed(self, index: int) -> None:
        if self.tabs.widget(index) is self.pcf_tab:
            self.pcf_tab.ensure_loaded()

    def populate_all(self, selected_id: str | None) -> None:
        assert self.result is not None
        self.populate_summary()
        self.populate_baskets(selected_id)
        self.populate_venue_closes()
        self.populate_account_transfers()
        self.populate_warnings()
        self.mapping_tab.update_data(self.result)
        self.pcf_tab.suggest_date(self.result.qmt_latest_day or date.today())
        self.refresh_details()

    def populate_summary(self) -> None:
        assert self.result is not None
        while self.summary_grid.count():
            item = self.summary_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        values = [
            ("篮子数量", f"{len(self.result.baskets)}"),
            ("已结算", f"{len(self.result.settled_baskets)}"),
            ("已结算收益 RMB", fmt_money(self.result.settled_total_cny)),
            ("未分配 IB", f"SELL {self.result.unallocated_ib_sell_qty:,} / BUY {self.result.unallocated_ib_buy_qty:,}"),
            ("手工休市日", f"{len(self.market_holidays())}"),
            ("跨账户调仓", f"{len(self.result.account_transfers)}"),
        ]
        for column, (name, value) in enumerate(values):
            field = QWidget()
            field.setObjectName("summaryFieldPositive" if name == "已结算收益 RMB" else "summaryField")
            layout = QVBoxLayout(field)
            layout.setContentsMargins(8, 5, 8, 5)
            key = QLabel(name)
            key.setObjectName("summaryKey")
            val = QLabel(value)
            val.setObjectName("summaryValuePositive" if name == "已结算收益 RMB" else "summaryValue")
            layout.addWidget(key)
            layout.addWidget(val)
            self.summary_grid.addWidget(field, 0, column)

    def populate_baskets(self, selected_id: str | None) -> None:
        assert self.result is not None
        detailed = self.detail_button.isChecked()
        if detailed:
            headers = [
                "轮次", "状态", "赎回日", "预计T+3", "实际T+3", "预计T+6到账", "实际到账",
                "来源", "合同编号", "份额", "国内成本", "退款", "现金差额", "国内盈亏",
                "IB股数", "IB净盈亏USD", "IB盈亏RMB", "合计RMB", "IB映射",
            ]
        else:
            headers = [
                "轮次", "状态", "赎回日", "T+3", "T+6到账", "来源", "份额", "国内成本",
                "退款", "现金差额", "国内盈亏", "IB股数", "IB净盈亏USD", "IB盈亏RMB",
                "合计RMB", "IB映射",
            ]
        rows = []
        payloads = []
        for basket in self.result.baskets:
            common_tail = [
                basket.source,
                f"{basket.redeem_qty:,}",
                fmt_money(basket.domestic_cost),
                fmt_money(basket.refund_amount),
                fmt_money(basket.cash_difference),
                fmt_money(basket.domestic_pnl),
                f"{basket.hedge_target - basket.ib_open_shortfall:,}/{basket.hedge_target:,}",
                fmt_decimal(basket.ib_pnl_usd),
                fmt_money(basket.ib_pnl_cny),
                fmt_money(basket.total_pnl_cny),
                "人工" if basket.manual_ib_mapping else "FIFO",
            ]
            if detailed:
                rows.append(
                    [
                        basket.sequence,
                        basket.status,
                        basket.redeem_day.isoformat(),
                        basket.expected_cash_difference_day.isoformat() if basket.expected_cash_difference_day else "--",
                        basket.actual_cash_difference_day.isoformat() if basket.actual_cash_difference_day else "--",
                        basket.expected_refund_day.isoformat() if basket.expected_refund_day else "--",
                        basket.actual_refund_day.isoformat() if basket.actual_refund_day else "--",
                        basket.source,
                        basket.contract_no,
                        *common_tail[1:],
                    ]
                )
            else:
                rows.append(
                    [
                        basket.sequence,
                        basket.status,
                        basket.redeem_day.isoformat(),
                        basket.expected_cash_difference_day.isoformat() if basket.expected_cash_difference_day else "--",
                        basket.expected_refund_day.isoformat() if basket.expected_refund_day else "--",
                        *common_tail,
                    ]
                )
            payloads.append(basket.id)
        fill_table(self.basket_table, headers, rows, payloads=payloads)
        status_colors = {
            "已结算": ("#ecfdf3", "#14532d"),
            "待现金差额": ("#fff7ed", "#9a3412"),
            "待现金替代款": ("#fffbeb", "#92400e"),
            "IB未完全匹配": ("#fef2f2", "#991b1b"),
            "国内库存不足": ("#fef2f2", "#991b1b"),
        }
        selected_row = 0
        for row, basket in enumerate(self.result.baskets):
            background, foreground = status_colors.get(basket.status, ("#ffffff", "#111827"))
            for column in range(self.basket_table.columnCount()):
                self.basket_table.item(row, column).setBackground(QColor(background))
                self.basket_table.item(row, column).setForeground(QColor(foreground))
            if basket.id == selected_id:
                selected_row = row
        if self.basket_table.rowCount():
            self.basket_table.selectRow(selected_row)
            self.basket_table.horizontalScrollBar().setValue(0)

    def populate_venue_closes(self) -> None:
        assert self.result is not None
        headers = ["日期", "账户", "合同编号", "数量", "卖出净额", "FIFO成本", "场内收益", "库存缺口"]
        rows = [
            [
                item.trade_day.isoformat(), item.source, item.contract_no, f"{item.qty:,}",
                fmt_money(item.proceeds), fmt_money(item.cost), fmt_money(item.pnl), f"{item.inventory_shortfall:,}",
            ]
            for item in self.result.venue_closes
        ]
        fill_table(self.venue_closes_table, headers, rows or [["--", "--", "--", "--", "--", "--", "暂无场内卖出", "--"]])

    def populate_account_transfers(self) -> None:
        assert self.result is not None
        headers = [
            "日期", "数量", "卖出账户", "卖出合同", "卖出净额", "原FIFO成本", "调仓已实现盈亏",
            "买入账户", "买入合同", "新买入成本", "合同号间隔", "库存缺口",
        ]
        rows = [
            [
                item.trade_day.isoformat(),
                f"{item.qty:,}",
                item.sell_source,
                item.sell_contract_no,
                fmt_money(item.sell_proceeds),
                fmt_money(item.sell_fifo_cost),
                fmt_money(item.realized_pnl),
                item.buy_source,
                item.buy_contract_no,
                fmt_money(item.buy_cost),
                item.contract_gap,
                f"{item.inventory_shortfall:,}",
            ]
            for item in self.result.account_transfers
        ]
        fill_table(
            self.transfer_table,
            headers,
            rows or [["--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "暂无自动识别的跨账户调仓"]],
        )

    def populate_warnings(self) -> None:
        assert self.result is not None
        rows: list[list[object]] = []
        for basket in self.result.baskets:
            for warning in basket.warnings:
                rows.append([basket.sequence, basket.redeem_day.isoformat(), basket.contract_no, basket.status, warning])
        rows.append(["全局", "--", "--", "未分配IB", f"SELL {self.result.unallocated_ib_sell_qty:,} 股；BUY {self.result.unallocated_ib_buy_qty:,} 股"])
        for warning in self.result.warnings:
            rows.append(["全局", "--", "--", "提示", warning])
        fill_table(self.warning_table, ["轮次", "日期", "合同编号", "类型", "说明"], rows)

    def selected_basket_id(self) -> str | None:
        row = self.basket_table.currentRow()
        if row < 0:
            return None
        item = self.basket_table.item(row, 0)
        value = item.data(Qt.UserRole) if item is not None else None
        return str(value) if value else None

    def selected_basket(self) -> engine.BasketResult | None:
        basket_id = self.selected_basket_id()
        return self.basket_by_id.get(basket_id or "")

    def open_basket_detail(self, _row: int, _column: int) -> None:
        basket = self.selected_basket()
        if basket is None:
            return
        BasketDetailDialog(basket, self).exec_()

    def refresh_details(self) -> None:
        basket = self.selected_basket()
        if basket is None:
            return
        domestic_rows = [
            [item.trade_day.isoformat(), item.source, item.contract_no, f"{item.qty:,}", fmt_money(item.cost)]
            for item in basket.domestic_matches
        ]
        fill_table(self.domestic_matches_table, ["买入日期", "来源账户", "买入合同", "使用数量", "分摊成本"], domestic_rows)
        self._fill_ib_table(self.ib_open_table, basket.ib_open)
        self._fill_ib_table(self.ib_close_table, basket.ib_close)
        cash_rows = [
            [item.trade_day.isoformat(), item.source, item.contract_no, item.action, fmt_money(item.amount), item.row_number]
            for item in basket.cash_flows
        ]
        cash_rows.insert(
            0,
            [
                basket.expected_refund_day.isoformat() if basket.expected_refund_day else "--",
                basket.source,
                basket.contract_no,
                "预计T+6到账日",
                "--",
                "休市日历计算",
            ],
        )
        fill_table(self.cash_table, ["日期", "账户", "合同编号", "业务", "金额", "源行号"], cash_rows or [["--", basket.source, basket.contract_no, "--", "暂无相关资金流水", "--"]])

    @staticmethod
    def _fill_ib_table(table: QTableWidget, slices: tuple[engine.IbSlice, ...]) -> None:
        rows = [
            [item.dt.strftime("%Y-%m-%d %H:%M:%S"), item.side, item.qty, fmt_decimal(item.price, 4), fmt_money(item.gross), fmt_decimal(item.commission), item.trade_id]
            for item in slices
        ]
        fill_table(table, ["时间（IB账单）", "方向", "数量", "价格", "成交额USD", "佣金USD", "交易ID"], rows)

    def edit_ib_mapping(self) -> None:
        if self.result is None:
            return
        basket = self.selected_basket()
        if basket is None:
            QMessageBox.information(self, "未选中", "请先选择一个篮子。")
            return
        override = self.overrides.get(basket.id, {})
        open_ids = set(override.get("open_trade_ids") or [item.trade_id for item in basket.ib_open])
        close_ids = set(override.get("close_trade_ids") or [item.trade_id for item in basket.ib_close])
        dialog = IbMappingDialog(basket, self.result.ib_trades, open_ids, close_ids, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        self.overrides[basket.id] = {
            "open_trade_ids": dialog.selected_ids(dialog.open_table),
            "close_trade_ids": dialog.selected_ids(dialog.close_table),
        }
        engine.save_overrides(OVERRIDES_PATH, self.overrides)
        self.calculate()

    def clear_ib_mapping(self) -> None:
        basket = self.selected_basket()
        if basket is None or basket.id not in self.overrides:
            return
        self.overrides.pop(basket.id, None)
        engine.save_overrides(OVERRIDES_PATH, self.overrides)
        self.calculate()

    def export_csv(self) -> None:
        if self.result is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, "导出篮子汇总", str(ROOT / "赎回收益汇总.csv"), "CSV (*.csv)")
        if not path:
            return
        rows = engine.basket_summary_rows(self.result)
        if not rows:
            return
        with Path(path).open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        self.status_label.setText(f"已导出：{path}")


def apply_light_theme(app: QApplication) -> None:
    app.setStyle(QStyleFactory.create("Fusion"))
    palette = QPalette()
    palette.setColor(QPalette.Window, QColor("#f4f7fb"))
    palette.setColor(QPalette.WindowText, QColor("#111827"))
    palette.setColor(QPalette.Base, QColor("#ffffff"))
    palette.setColor(QPalette.AlternateBase, QColor("#f8fbff"))
    palette.setColor(QPalette.Text, QColor("#111827"))
    palette.setColor(QPalette.Button, QColor("#ffffff"))
    palette.setColor(QPalette.ButtonText, QColor("#111827"))
    palette.setColor(QPalette.Highlight, QColor("#dbeafe"))
    palette.setColor(QPalette.HighlightedText, QColor("#111827"))
    app.setPalette(palette)
    app.setStyleSheet(
        """
        QMainWindow, QWidget { color: #111827; }
        QGroupBox { border: 1px solid #dbe4f0; border-radius: 10px; margin-top: 8px; background: rgba(255,255,255,0.94); }
        QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #6b7280; }
        QTabWidget::pane { border: 1px solid #dbe4f0; border-radius: 8px; background: #ffffff; }
        QTabBar::tab { background: #eef3f8; border: 1px solid #dbe4f0; border-bottom: none; border-top-left-radius: 7px; border-top-right-radius: 7px; padding: 5px 12px; margin-right: 2px; }
        QTabBar::tab:selected { background: #ffffff; }
        QTableWidget { background: #ffffff; alternate-background-color: #f8fbff; border: 1px solid #dbe4f0; border-radius: 8px; gridline-color: #edf2f7; selection-background-color: #dbeafe; selection-color: #111827; font-family: "PingFang SC", "Microsoft YaHei UI"; font-size: 13px; }
        QTableWidget::item { padding: 3px 6px; }
        QHeaderView::section { background: #f8fafc; color: #374151; padding: 5px 6px; border: none; border-bottom: 1px solid #e5e7eb; border-right: 1px solid #eef2f7; }
        QPushButton { background: #ffffff; border: 1px solid #cfd8e3; border-radius: 8px; padding: 5px 12px; }
        QPushButton:hover { background: #f3f7ff; border-color: #b7c7da; }
        QLineEdit, QDoubleSpinBox { background: #ffffff; border: 1px solid #cfd8e3; border-radius: 6px; padding: 4px 6px; }
        QLabel#sourceHint { color: #6b7280; font-size: 12px; }
        QLabel#summaryKey { color: #6b7280; font-size: 10px; }
        QLabel#summaryKeyAccent { color: #c2410c; font-size: 10px; font-weight: 600; }
        QLabel#summaryValue { color: #111827; font-weight: 600; }
        QLabel#summaryValueAccent { color: #9a3412; font-weight: 700; }
        QLabel#summaryValuePositive { color: #14532d; font-weight: 700; }
        QWidget#summaryField { background: #ffffff; border: 1px solid #e5eaf2; border-radius: 6px; }
        QWidget#summaryFieldAccent { background: #fff7ed; border: 1px solid #fdba74; border-radius: 6px; }
        QWidget#summaryFieldPositive { background: #ecfdf3; border: 1px solid #86efac; border-radius: 6px; }
        QWidget#fxMetricCard { background: #f8fafc; border: 1px solid #e5eaf2; border-radius: 8px; }
        QWidget#fxMetricCardAccent { background: #eff6ff; border: 1px solid #93c5fd; border-radius: 8px; }
        QLabel#fxMetricKey { color: #6b7280; font-size: 10px; }
        QLabel#fxMetricKeyAccent { color: #1d4ed8; font-size: 10px; font-weight: 600; }
        QLabel#fxMetricValue { color: #111827; font-size: 15px; font-weight: 700; }
        QLabel#fxMetricValueAccent { color: #1d4ed8; font-size: 15px; font-weight: 700; }
        """
    )


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("ETF 赎回收益计算器")
    apply_light_theme(app)
    font = QFont("PingFang SC")
    font.setPointSize(11)
    app.setFont(font)
    window = MainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
