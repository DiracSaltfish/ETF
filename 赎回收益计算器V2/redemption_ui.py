from __future__ import annotations

import csv
import json
import os
import sys
import threading
import traceback
from collections import defaultdict
from datetime import date, datetime, time as clock_time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from PyQt5.QtCore import QDate, QFileSystemWatcher, QObject, QPoint, QPointF, Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QFontDatabase, QPainter, QPainterPath, QPalette, QPen
from PyQt5.QtWidgets import (
    QAbstractItemView,
    QAction,
    QApplication,
    QCalendarWidget,
    QCheckBox,
    QComboBox,
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
import backfill_xop_from_tws
import basket_calibration
import fx_rates
import market_data
import realtime_premium
import settlement_estimator
import szse_pcf
import xop_close_orders


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
OVERRIDES_PATH = ROOT / "ib_mapping_overrides.json"
BEIJING = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


DEFAULT_CONFIG = {
    "qmt1_path": "",
    "qmt2_path": "",
    "qmt3_path": "",
    "ib_path": "",
    "fx_rate": "6.79635",
    "auto_refresh": True,
    "market_holidays": [],
    "transfer_contract_gap": 1000,
    "szse_pcf_cache_dir": str(ROOT / "szse_pcf_cache"),
    "fx_rates_csv_path": str(ROOT / "fx_data" / "fx_rates.csv"),
    "xop_price_csv_path": str(ROOT / "market_data" / "xop_prices.csv"),
    "calibration_csv_path": str(ROOT / "calibration" / "pcf_calibration_points.csv"),
    "settlement_observation_csv_path": str(ROOT / "calibration" / "settlement_observations.csv"),
    "predicted_refund_csv_path": str(ROOT / "calibration" / "predicted_refunds.csv"),
    "estimate_price_window": "1540_1600",
    "tws_host": "127.0.0.1",
    "tws_port": 7496,
    "tws_client_id": 8888,
    "tws_auto_client_id": True,
    "shared_folder_path": "",
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
    for key in (
        "szse_pcf_cache_dir",
        "fx_rates_csv_path",
        "xop_price_csv_path",
        "calibration_csv_path",
        "settlement_observation_csv_path",
        "predicted_refund_csv_path",
    ):
        if not str(config.get(key) or "").strip():
            config[key] = DEFAULT_CONFIG[key]
    return config


def save_config(config: dict[str, object]) -> None:
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")


def input_path_errors(qmt_paths: dict[str, str | None], ib_path: str) -> list[str]:
    errors: list[str] = []
    required = (("QMT1", qmt_paths.get("QMT1")), ("IB", ib_path))
    optional = (("QMT2", qmt_paths.get("QMT2")), ("QMT3", qmt_paths.get("QMT3")))
    for label, value in required:
        text = str(value or "").strip()
        if not text:
            errors.append(f"{label}未配置")
        elif not Path(text).expanduser().is_file():
            errors.append(f"{label}文件不存在：{text}")
    for label, value in optional:
        text = str(value or "").strip()
        if text and not Path(text).expanduser().is_file():
            errors.append(f"{label}文件不存在：{text}")
    return errors


def fmt_money(value: Decimal) -> str:
    return f"{engine.money(value):,.2f}"


def fmt_decimal(value: Decimal, places: int = 6) -> str:
    return f"{value:,.{places}f}"


def basket_summary_date_text(value: date | None, detailed: bool) -> str:
    if value is None:
        return "--"
    return value.isoformat() if detailed else value.strftime("%m-%d")


def ib_mapping_text(basket: engine.BasketResult) -> str:
    if basket.manual_virtual_close:
        return "人工虚拟"
    if basket.domestic_rollover_open:
        return "滚动承接"
    if any(item.role == "domestic_rollover_close" for item in basket.ib_close):
        return "自动虚拟"
    if basket.manual_ib_mapping:
        return "人工"
    return "FIFO"


def refund_amount_text(basket: engine.BasketResult) -> str:
    text = fmt_money(basket.refund_amount)
    if basket.manual_refund_applied:
        return text
    if basket.manual_refund_amount is not None and basket.actual_refund_day is not None:
        return f"{text}（交割单）"
    return text


def price_window_text(value: str) -> str:
    return {
        "1540_1550": "美东时间15:40–15:50成交量加权平均价",
        "1540_1600": "美东时间15:40–16:00成交量加权平均价",
        "1554_1557": "美东时间15:54–15:57成交量加权平均价",
        "1559_close": "美东时间15:59一分钟收盘价",
    }.get(value, value)


def confidence_text(value: str) -> str:
    return {"high": "高", "medium": "中", "low": "低"}.get(value, value)


def calibration_method_text(value: str) -> str:
    return {
        "pcf_net": "采用扣除PCF现金差额后的反推股数",
    }.get(value, value)


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
    for column_index, header in enumerate(headers):
        header_item = table.horizontalHeaderItem(column_index)
        if header_item is not None:
            header_item.setToolTip(header)
    table.setRowCount(len(rows))
    for row_index, row in enumerate(rows):
        for column_index, value in enumerate(row):
            item = QTableWidgetItem(str(value))
            item.setToolTip(str(value))
            if payloads is not None and column_index == 0:
                item.setData(Qt.UserRole, payloads[row_index])
            if isinstance(value, (int, float, Decimal)):
                item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
            table.setItem(row_index, column_index, item)
    table.resizeColumnsToContents()
    for column in range(table.columnCount()):
        if table.columnWidth(column) > 240:
            table.setColumnWidth(column, 240)


def fill_explanation_table(
    table: QTableWidget,
    headers: list[str],
    rows: list[list[object]],
) -> None:
    """Three-column result table optimized for complete, non-abbreviated labels."""
    fill_table(table, headers, rows)
    if table.columnCount() >= 3:
        table.setColumnWidth(0, 430)
        table.setColumnWidth(1, 230)
        table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)


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
    "PreCashComponent": "PreTradingDay",
    "EstimatedCashComponent": "TradingDay",
    "CreationLimit": "TradingDay",
    "NetCreationLimit": "TradingDay",
    "NetRedemptionLimit": "TradingDay",
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

    def __init__(
        self,
        cache_root: Path,
        fx_csv_path: Path,
        trading_day: date,
        fund_code: str,
        force_refresh: bool,
    ) -> None:
        super().__init__()
        self.cache_root = cache_root
        self.fx_csv_path = fx_csv_path
        self.trading_day = trading_day
        self.exchange, parsed_code = szse_pcf.parse_fund_key(fund_code)
        self.fund_code = parsed_code or szse_pcf.TARGET_FUND_CODE
        self.force_refresh = force_refresh
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        store = szse_pcf.SzsePcfStore(self.cache_root)
        fx_store = fx_rates.FxRateStore(self.fx_csv_path)
        result = {
            "trading_day": self.trading_day,
            "exchange": self.exchange,
            "fund_code": self.fund_code,
            "force_refresh": self.force_refresh,
            "index": None,
            "index_error": "",
            "detail": None,
            "detail_error": "",
            "fx_hours": [],
            "fx_matrix": [],
            "fx_error": "",
            "cancelled": False,
        }
        result["index"] = store.build_focus_day_index(self.trading_day)

        if self._cancel_event.is_set():
            result["cancelled"] = True
            self.finished.emit(result)
            return

        try:
            fx_store.ensure_trade_date(self.trading_day, force_refresh=self.force_refresh)
            hours, matrix = fx_store.build_day_matrix(self.trading_day)
            result["fx_hours"] = hours
            result["fx_matrix"] = matrix
        except Exception as exc:
            result["fx_error"] = str(exc)

        if not self._cancel_event.is_set():
            try:
                result["detail"] = store.ensure_fund_detail(
                    self.trading_day,
                    self.fund_code,
                    force_refresh=self.force_refresh,
                    exchange=self.exchange,
                )
            except Exception as exc:
                result["detail_error"] = str(exc)

        result["cancelled"] = self._cancel_event.is_set()

        self.finished.emit(result)


class PcfPrefetchWorker(QObject):
    progress = pyqtSignal(object)
    finished = pyqtSignal(object)

    def __init__(self, cache_root: Path, trading_day: date, items: tuple[szse_pcf.PcfListItem, ...]) -> None:
        super().__init__()
        self.cache_root = cache_root
        self.trading_day = trading_day
        self.items = items
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        store = szse_pcf.SzsePcfStore(self.cache_root)
        completed: list[str] = []
        failures: list[str] = []
        ordered_items = szse_pcf.interleave_pcf_items(self.items)
        total = len(ordered_items)
        for position, item in enumerate(ordered_items, start=1):
            if self._cancel_event.is_set():
                break
            key = szse_pcf.fund_key(item.exchange, item.fund_code)
            self.progress.emit(
                {
                    "trading_day": self.trading_day,
                    "key": key,
                    "position": position,
                    "total": total,
                    "state": "loading",
                }
            )
            try:
                store.ensure_fund_detail(self.trading_day, item.fund_code, exchange=item.exchange)
            except Exception as exc:
                failures.append(f"{key}: {exc}")
                state = "failed"
            else:
                completed.append(key)
                state = "cached"
            self.progress.emit(
                {
                    "trading_day": self.trading_day,
                    "key": key,
                    "position": position,
                    "total": total,
                    "state": state,
                }
            )
        self.finished.emit(
            {
                "trading_day": self.trading_day,
                "total": total,
                "completed": completed,
                "failures": failures,
            }
        )
        QThread.currentThread().quit()


class PredictedRefundWorker(QObject):
    finished = pyqtSignal(object)

    def __init__(
        self,
        baskets: tuple[engine.BasketResult, ...],
        xop_csv_path: Path,
        fx_csv_path: Path,
        pcf_cache_path: Path,
        predicted_refund_csv_path: Path,
        host: str,
        port: int,
        client_id: int,
    ) -> None:
        super().__init__()
        self.baskets = baskets
        self.xop_csv_path = xop_csv_path
        self.fx_csv_path = fx_csv_path
        self.pcf_cache_path = pcf_cache_path
        self.predicted_refund_csv_path = predicted_refund_csv_path
        self.host = host
        self.port = port
        self.client_id = client_id
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(self) -> None:
        payload: dict[str, object] = {"predictions": [], "errors": [], "fatal_error": ""}
        try:
            if not self.baskets:
                self.finished.emit(payload)
                return
            days = sorted({basket.redeem_day for basket in self.baskets})
            prices = backfill_xop_from_tws.fetch_prices(
                days[0],
                days[-1],
                host=self.host,
                port=self.port,
                client_id=self.client_id,
                intraday_days=set(days),
            )
            if self._cancel_event.is_set():
                payload["cancelled"] = True
                self.finished.emit(payload)
                return
            market_data.upsert_xop_prices(self.xop_csv_path, prices)
            price_provider = market_data.CsvXopPriceProvider(self.xop_csv_path)
            fx_store = fx_rates.FxRateStore(self.fx_csv_path)
            pcf_store = szse_pcf.SzsePcfStore(self.pcf_cache_path)
            store = settlement_estimator.PredictedRefundStore(self.predicted_refund_csv_path)
            now = datetime.now().isoformat(timespec="seconds")
            predictions: list[settlement_estimator.PredictedRefund] = []
            errors: list[str] = []
            pcf_cash_by_day: dict[date, Decimal] = {}
            for basket in self.baskets:
                if self._cancel_event.is_set():
                    break
                try:
                    day_price = price_provider.get_daily_price(basket.redeem_day)
                    xop_price = day_price.last_1559
                    if xop_price is None:
                        raise ValueError("缺少XOP美东15:59一分钟收盘价")
                    fx_quote_time = settlement_estimator.PREDICTED_BASKET_FX_QUOTE_TIME
                    settlement_fx = fx_store.get_usd_cny_cfets_hour(basket.redeem_day, fx_quote_time)
                    if settlement_fx is None:
                        fx_store.refresh_cfets_date(basket.redeem_day)
                        settlement_fx = fx_store.get_usd_cny_cfets_hour(basket.redeem_day, fx_quote_time)
                    if settlement_fx is None:
                        raise ValueError(f"缺少T日CFETS {fx_quote_time}美元人民币参考价")
                    pcf_cash_component = pcf_cash_by_day.get(basket.redeem_day)
                    if pcf_cash_component is None:
                        detail = pcf_store.ensure_target_detail(basket.redeem_day)
                        raw_cash_component = str(detail.metadata.get("EstimateCashComponent") or "").strip()
                        if not raw_cash_component:
                            raise ValueError("当日PCF缺少EstimateCashComponent")
                        pcf_cash_component = Decimal(raw_cash_component)
                        pcf_cash_by_day[basket.redeem_day] = pcf_cash_component
                    predictions.append(
                        settlement_estimator.estimate_predicted_refund(
                            basket,
                            xop_price,
                            settlement_fx,
                            calculated_at=now,
                            pcf_estimate_cash_component_cny=pcf_cash_component,
                        )
                    )
                except Exception as exc:
                    errors.append(f"篮子{basket.sequence} {basket.redeem_day:%Y-%m-%d}: {exc}")
            cancelled = self._cancel_event.is_set()
            if predictions and not cancelled:
                store.append_or_replace_many(predictions)
            payload["predictions"] = predictions
            payload["errors"] = errors
            payload["cancelled"] = cancelled
        except Exception as exc:
            payload["fatal_error"] = str(exc)
        self.finished.emit(payload)


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


class DirectoryPicker(QWidget):
    def __init__(self, title: str, value: str) -> None:
        super().__init__()
        self.title = title
        self.edit = QLineEdit(value)
        self.button = QPushButton("选择")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.button.clicked.connect(self.choose)

    def choose(self) -> None:
        path = QFileDialog.getExistingDirectory(self, self.title, self.edit.text())
        if path:
            self.edit.setText(path)

    def value(self) -> str:
        return self.edit.text().strip()


class SettingsDialog(QDialog):
    def __init__(self, config: dict[str, object], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("数据源设置")
        self.resize(820, 400)
        self.qmt1 = FilePicker("选择 QMT1 完整交割单", str(config.get("qmt1_path") or ""), "Excel (*.xlsx *.xls)")
        self.qmt2 = FilePicker("选择 QMT2 完整交割单", str(config.get("qmt2_path") or ""), "Excel (*.xlsx *.xls)")
        self.qmt3 = FilePicker("选择 QMT3 完整交割单", str(config.get("qmt3_path") or ""), "Excel (*.xlsx *.xls)")
        self.qmt3.setToolTip("QMT3只计入159518库存、场内自平和移仓成本；不会生成赎回篮子或读取退款流水。")
        self.ib = FilePicker("选择 IB 完整成交汇总", str(config.get("ib_path") or ""), "CSV (*.csv)")
        self.transfer_gap = QSpinBox()
        self.transfer_gap.setRange(0, 1_000_000)
        self.transfer_gap.setValue(int(config.get("transfer_contract_gap") or engine.DEFAULT_TRANSFER_CONTRACT_GAP))
        self.transfer_gap.setSuffix(" 号")
        self.transfer_gap.setToolTip("无成交时间时，用合同编号差作为邻近成交代理；识别同日、跨账户、方向相反、数量完全相同的组合，买卖先后均可。")
        self.tws_host = QLineEdit(str(config.get("tws_host") or "127.0.0.1"))
        self.tws_port = QSpinBox()
        self.tws_port.setRange(1, 65535)
        self.tws_port.setValue(int(config.get("tws_port") or 7496))
        self.tws_client_id = QSpinBox()
        self.tws_client_id.setRange(0, 2_147_483_647)
        self.tws_client_id.setValue(int(config.get("tws_client_id") or 8888))
        self.tws_auto_client_id = QCheckBox("启动时自动分配；若遇到 326 占号则继续自动切号")
        self.tws_auto_client_id.setChecked(bool(config.get("tws_auto_client_id", True)))
        self.tws_auto_client_id.setToolTip(
            "启用时根据当前程序进程生成低碰撞 Client ID；关闭时先用下方手工 ID。"
            "两种模式遇到 TWS 错误 326 都会自动换号。"
        )
        self.shared_folder = DirectoryPicker(
            "选择共享文件夹目录",
            str(config.get("shared_folder_path") or ""),
        )
        form = QFormLayout()
        form.addRow("QMT1", self.qmt1)
        form.addRow("QMT2（可空）", self.qmt2)
        form.addRow("QMT3（可空，仅成本/移仓）", self.qmt3)
        form.addRow("IB", self.ib)
        form.addRow("调仓合同号最大间隔", self.transfer_gap)
        form.addRow("TWS主机", self.tws_host)
        form.addRow("TWS端口", self.tws_port)
        form.addRow("TWS Client ID 模式", self.tws_auto_client_id)
        form.addRow("手工首选 Client ID", self.tws_client_id)
        form.addRow("共享文件夹目录路径", self.shared_folder)
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
            "qmt3_path": self.qmt3.value(),
            "ib_path": self.ib.value(),
            "transfer_contract_gap": self.transfer_gap.value(),
            "tws_host": self.tws_host.text().strip() or "127.0.0.1",
            "tws_port": self.tws_port.value(),
            "tws_client_id": self.tws_client_id.value(),
            "tws_auto_client_id": self.tws_auto_client_id.isChecked(),
            "shared_folder_path": self.shared_folder.value(),
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
        note = QLabel("休市日只影响 T+6 现金替代款预计到账日；T+3 现金差额日按境内工作日计算，周六、周日始终自动跳过。")
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
        owner: engine.BasketResult | engine.VenueClose,
        trades: tuple[engine.IbTrade, ...],
        open_ids: set[str],
        close_ids: set[str],
        parent=None,
    ) -> None:
        super().__init__(parent)
        if isinstance(owner, engine.BasketResult):
            title = f"第 {owner.sequence} 篮子 IB 人工指定"
        else:
            title = f"{owner.trade_day:%Y-%m-%d} {owner.source} 碎单自平 IB 人工指定"
        self.setWindowTitle(title)
        self.resize(1180, 720)
        self.target = owner.hedge_target
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
    def __init__(
        self,
        basket: engine.BasketResult,
        virtual_close_enabled: bool = False,
        manual_refund_amount: Decimal | None = None,
        save_manual_overrides=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.basket = basket
        self.save_manual_overrides_callback = save_manual_overrides
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
            ("退款/现金差额", f"{refund_amount_text(basket)} / {fmt_money(basket.cash_difference)} RMB"),
            ("国内盈亏", f"{fmt_money(basket.domestic_pnl)} RMB"),
            ("IB净盈亏", f"{fmt_decimal(basket.ib_pnl_usd)} USD"),
            ("全局汇率", fmt_decimal(basket.fx_rate, 6)),
            ("合计盈亏", f"{fmt_money(basket.total_pnl_cny)} RMB"),
            ("IB映射", ib_mapping_text(basket)),
            ("IB目标", f"{basket.hedge_target:,} 股"),
            (
                "QMT3承接",
                f"{sum(item.qty for item in basket.qmt3_hedge_open):,}/{basket.qmt3_hedge_target:,} 股",
            ),
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

        override_box = QGroupBox("人工口径")
        override_layout = QVBoxLayout(override_box)
        self.virtual_close_check = QCheckBox("该篮子使用次交易日国内买入作为虚拟平仓")
        self.virtual_close_check.setChecked(virtual_close_enabled)
        self.manual_refund_check = QCheckBox("交割单未出现ETF申购退款时，使用人工退款金额")
        self.manual_refund_spin = QDoubleSpinBox()
        self.manual_refund_spin.setDecimals(2)
        self.manual_refund_spin.setRange(0, 10_000_000_000)
        self.manual_refund_spin.setSuffix(" RMB")
        self.manual_refund_spin.setSingleStep(1000)
        if manual_refund_amount is not None:
            self.manual_refund_check.setChecked(True)
            self.manual_refund_spin.setValue(float(manual_refund_amount))
        self.manual_refund_spin.setEnabled(self.manual_refund_check.isChecked())
        refund_row = QHBoxLayout()
        refund_row.addWidget(self.manual_refund_check)
        refund_row.addWidget(self.manual_refund_spin)
        refund_row.addStretch(1)
        note = QLabel(
            "人工退款只在交割单没有 ETF 申购退款流水时参与计算；"
            "一旦新交割单出现该篮子实际退款，程序自动以交割单为准。"
            "虚拟平仓与人工退款都会保存到 ib_mapping_overrides.json 并重新计算。"
        )
        note.setWordWrap(True)
        override_layout.addWidget(self.virtual_close_check)
        override_layout.addLayout(refund_row)
        override_layout.addWidget(note)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Save).setText("保存标记并重算")
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.accepted.connect(self.save_manual_overrides)
        buttons.rejected.connect(self.reject)
        self.manual_refund_check.toggled.connect(self.manual_refund_spin.setEnabled)
        layout = QVBoxLayout(self)
        layout.addWidget(summary_box)
        layout.addWidget(override_box)
        layout.addWidget(tabs, 1)
        layout.addWidget(buttons)

    def save_manual_overrides(self) -> None:
        manual_refund: Decimal | None = None
        if self.manual_refund_check.isChecked():
            manual_refund = engine.money(Decimal(str(self.manual_refund_spin.value())))
            if manual_refund <= 0:
                QMessageBox.warning(self, "人工退款金额无效", "人工ETF申购退款金额必须大于0。")
                return
        if self.save_manual_overrides_callback is not None:
            self.save_manual_overrides_callback(
                self.basket.id,
                self.virtual_close_check.isChecked(),
                manual_refund,
            )
        self.accept()

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
                    f"{sum(hedge.qty for hedge in item.qmt3_hedge_open):,}/{item.qmt3_hedge_target:,}",
                ]
            )
        fill_table(
            self.domestic_table,
            ["序号", "来源账户", "买入日期", "买入合同", "采用数量", "单位成本", "分摊成本RMB", "QMT3承接IB"],
            rows or [["--", "--", "--", "--", "--", "--", "--", "暂无国内买入匹配"]],
        )

    @staticmethod
    def _populate_ib(table: QTableWidget, slices: tuple[engine.IbSlice, ...]) -> None:
        rows = [
            [
                index,
                item.dt.strftime("%Y-%m-%d %H:%M:%S"),
                item.side,
                item.role or "--",
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
            ["序号", "时间（IB账单）", "方向", "角色", "采用数量", "价格", "分摊成交额USD", "分摊佣金USD", "交易ID"],
            rows or [["--", "--", "--", "--", "--", "--", "--", "--", "暂无IB匹配"]],
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
        self.venue_by_id: dict[str, engine.VenueClose] = {}
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
        self.legend = QLabel("篮子同色关联  ·  橙色=跨账户调仓（含QMT3成本承接）  ·  灰色=国内自平  ·  红色=IB自平  ·  白色=未闭合")
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
    def _record_key(source: str, trade_day: date, contract_no: int | str) -> tuple[str, date, int | str]:
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
        seq_by_key: dict[tuple[str, date, int | str], int] = {}
        day_counts: dict[date, int] = defaultdict(int)
        for record in trades:
            day_counts[record.trade_day] += 1
            seq_by_key[self._record_key(record.source, record.trade_day, record.contract_no)] = day_counts[record.trade_day]

        allocations: dict[tuple[str, date, int | str], list[tuple[str, str, int, str]]] = defaultdict(list)
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
                allocations[key].append(("transfer", group, match.qty, transfer.kind))
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
            if item.buy_source
        }
        transfer_labels = {
            f"transfer-{index}": item.kind
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
                    transfer_note = f"  [{transfer_labels[transfer_buys[key]]}买入腿]" if key in transfer_buys else ""
                    rows.append({
                        "sort": (record.trade_day, engine.contract_sort_key(record.contract_no), suborder),
                        "text": f"{base}  买入  {qty:,}  @{price:.4f}  → {label}{transfer_note}",
                        "kind": kind, "group": group, "special_group": transfer_buys.get(key, ""),
                    })
                remaining = max(0, record.qty - used)
                if remaining:
                    suborder += 1
                    transfer_note = f"  [{transfer_labels[transfer_buys[key]]}买入腿]" if key in transfer_buys else ""
                    rows.append({
                        "sort": (record.trade_day, engine.contract_sort_key(record.contract_no), suborder),
                        "text": f"{base}  买入  {remaining:,}  @{price:.4f}  [未分配库存]{transfer_note}",
                        "kind": "unallocated", "group": "", "special_group": transfer_buys.get(key, ""),
                    })
                continue
            suborder += 1
            kind = "transfer" if key in transfer_sells else "venue"
            group = transfer_sells.get(key) or venue_sells.get(key, "")
            label = f"{transfer_labels[group]}卖出腿" if kind == "transfer" else "国内自买自平"
            rows.append({
                "sort": (record.trade_day, engine.contract_sort_key(record.contract_no), suborder),
                "text": f"{base}  卖出  {record.qty:,}  @{price:.4f}  [{label}]",
                "kind": kind, "group": group, "special_group": "",
            })
        return sorted(rows, key=lambda item: item["sort"])

    def _ib_rows(self, start: date, end: date) -> list[dict[str, object]]:
        assert self.result is not None
        rows: list[dict[str, object]] = []
        order = 0
        for basket in self.result.baskets:
            for item in (*basket.ib_open, *basket.ib_close):
                if start <= item.dt.date() <= end:
                    order += 1
                    rows.append({
                        "sort": (item.dt, order),
                        "text": f"{item.dt:%m-%d %H:%M:%S}  {item.side:<4}  {item.qty:,}  @{item.price:.4f}  {item.role or '--'}  → 篮子{basket.sequence}",
                        "kind": "basket", "group": basket.id,
                    })

        for pair in self.result.ib_self_closes:
            group = f"ib-self-{pair.sequence}"
            label = "未归因IB残余配对"
            for current, role in ((pair.opening, "开"), (pair.closing, "平")):
                if start <= current.dt.date() <= end:
                    order += 1
                    rows.append({
                        "sort": (current.dt, order),
                        "text": f"{current.dt:%m-%d %H:%M:%S}  {current.side:<4}  {current.qty:,}  @{current.price:.4f}  [{label}{role}]",
                        "kind": "ib_self", "group": group,
                    })
        for item in self.result.unmatched_ib:
            if start <= item.dt.date() <= end:
                order += 1
                rows.append({
                    "sort": (item.dt, order),
                    "text": f"{item.dt:%m-%d %H:%M:%S}  {item.side:<4}  {item.qty:,}  @{item.price:.4f}  [未闭合]",
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
            mapping = ib_mapping_text(basket)
            text = (
                f"轮次 {basket.sequence}  |  {basket.status}\n"
                f"赎回  {basket.redeem_day:%Y-%m-%d}  |  {basket.source}\n"
                f"份额  {basket.redeem_qty:,}  |  合同 {basket.contract_no}\n"
                f"国内  成本 {fmt_money(basket.domestic_cost)}  |  盈亏 {fmt_money(basket.domestic_pnl)}\n"
                f"回款  退款 {refund_amount_text(basket)}  |  现金差额 {fmt_money(basket.cash_difference)}\n"
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


class IbSelfCloseTab(QWidget):
    """Domestic venue sale plus its foreign hedge, with residual IB as diagnostics."""

    mappingRequested = pyqtSignal(str)
    mappingCleared = pyqtSignal(str)

    STRATEGY_HEADERS = [
        "状态", "日期", "账户", "合同编号", "国内份额", "IB开/目标/平",
        "开仓时间", "平仓时间", "持有时长", "FIFO成本", "卖出净额",
        "国内盈亏RMB", "IB开仓均价", "IB平仓均价", "佣金USD",
        "IB盈亏USD", "IB盈亏RMB", "合计RMB", "配对",
    ]
    RESIDUAL_HEADERS = [
        "序号", "类型", "开仓时间", "平仓时间", "持有时长", "数量",
        "开仓价", "平仓价", "佣金USD", "诊断盈亏USD", "诊断盈亏RMB",
    ]
    UNMATCHED_HEADERS = ["时间", "方向", "数量", "价格", "原始角色", "状态"]

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.result: engine.CalculationResult | None = None
        self.metric_fields: dict[str, QWidget] = {}
        self.metric_values: dict[str, QLabel] = {}

        guide = QLabel(
            "口径：每一组自平必须同时包含国内159518场内卖出及其对应的XOP做空开仓、买入平仓。"
            "国内份额和IB成交均按数量切片唯一占用；已进入赎回篮子的数量不能重复计入自平。"
            "支持跨日持仓，跨日回补可人工指定。盈亏含IB佣金，暂不重新分摊借券费。"
        )
        guide.setObjectName("calculationGuide")
        guide.setWordWrap(True)

        metrics = QHBoxLayout()
        metrics.setSpacing(8)
        for key, title, tone in (
            ("completed", "已完整闭合", "normal"),
            ("domestic", "国内盈亏 RMB", "positive"),
            ("ib", "IB盈亏 USD / RMB", "positive"),
            ("total", "策略自平合计 RMB", "positive"),
            ("incomplete", "未完整 / 未归因", "accent"),
        ):
            metrics.addWidget(self._metric_card(key, title, tone), 1)

        self.strategy_table = configured_table()
        self.strategy_table.setAlternatingRowColors(False)
        self.strategy_table.setSortingEnabled(False)
        self.strategy_table.itemSelectionChanged.connect(self._refresh_selected_details)
        # Compatibility for existing callers/tests that referenced self_table.
        self.self_table = self.strategy_table

        self.domestic_table = configured_table()
        self.ib_open_table = configured_table()
        self.ib_close_table = configured_table()
        self.residual_table = configured_table()
        self.unmatched_table = configured_table()
        for table in (
            self.domestic_table,
            self.ib_open_table,
            self.ib_close_table,
            self.residual_table,
            self.unmatched_table,
        ):
            table.setAlternatingRowColors(False)
            table.setSortingEnabled(False)

        self.mapping_button = QPushButton("人工指定所选自平 IB 成交")
        self.clear_mapping_button = QPushButton("恢复所选自平自动配对")
        self.mapping_button.setEnabled(False)
        self.clear_mapping_button.setEnabled(False)
        self.mapping_button.clicked.connect(self._request_mapping)
        self.clear_mapping_button.clicked.connect(self._clear_mapping)
        controls = QHBoxLayout()
        controls.addWidget(self.mapping_button)
        controls.addWidget(self.clear_mapping_button)
        controls.addStretch(1)

        strategy_box = QGroupBox("国内外碎单自平（国内卖出 + XOP开平仓）")
        strategy_layout = QVBoxLayout(strategy_box)
        strategy_layout.setContentsMargins(8, 12, 8, 8)
        strategy_layout.addLayout(controls)
        strategy_layout.addWidget(self.strategy_table)

        detail_tabs = QTabWidget()
        detail_tabs.addTab(self.domestic_table, "所选自平国内FIFO来源")
        detail_tabs.addTab(self.ib_open_table, "所选自平IB开仓")
        detail_tabs.addTab(self.ib_close_table, "所选自平IB平仓")
        detail_tabs.addTab(self.residual_table, "未归因IB残余配对（诊断）")
        detail_tabs.addTab(self.unmatched_table, "未闭合风险头寸")
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(strategy_box)
        splitter.addWidget(detail_tabs)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(guide)
        layout.addLayout(metrics)
        layout.addWidget(splitter, 1)

    def _metric_card(self, key: str, title: str, tone: str) -> QWidget:
        field = QWidget()
        field.setObjectName({"positive": "summaryFieldPositive", "accent": "summaryFieldAccent"}.get(tone, "summaryField"))
        layout = QVBoxLayout(field)
        layout.setContentsMargins(10, 7, 10, 7)
        label = QLabel(title)
        label.setObjectName("summaryKeyAccent" if tone == "accent" else "summaryKey")
        value = QLabel("--")
        value.setObjectName({"positive": "summaryValuePositive", "accent": "summaryValueAccent"}.get(tone, "summaryValue"))
        layout.addWidget(label)
        layout.addWidget(value)
        self.metric_fields[key] = field
        self.metric_values[key] = value
        return field

    @staticmethod
    def _duration_text(opening: datetime, closing: datetime) -> str:
        seconds = max(0, int((closing - opening).total_seconds()))
        days, seconds = divmod(seconds, 86_400)
        hours, seconds = divmod(seconds, 3_600)
        minutes, seconds = divmod(seconds, 60)
        if days:
            return f"{days}天 {hours:02d}:{minutes:02d}:{seconds:02d}"
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    def _set_pnl_metric(self, key: str, value: Decimal, text: str) -> None:
        positive = value >= 0
        field_name = "summaryFieldPositive" if positive else "summaryFieldNegative"
        value_name = "summaryValuePositive" if positive else "summaryValueNegative"
        field = self.metric_fields[key]
        label = self.metric_values[key]
        field.setObjectName(field_name)
        label.setObjectName(value_name)
        label.setText(text)
        field.style().unpolish(field)
        field.style().polish(field)
        label.style().unpolish(label)
        label.style().polish(label)

    @staticmethod
    def _average_price(slices: tuple[engine.IbSlice, ...]) -> Decimal:
        qty = sum(item.qty for item in slices)
        return sum((item.gross for item in slices), Decimal("0")) / Decimal(qty) if qty else Decimal("0")

    @staticmethod
    def _fill_ib_slices(table: QTableWidget, slices: tuple[engine.IbSlice, ...]) -> None:
        rows = [
            [
                item.dt.strftime("%Y-%m-%d %H:%M:%S"),
                item.side,
                item.role or "--",
                f"{item.qty:,}",
                fmt_decimal(item.price, 4),
                fmt_money(item.gross),
                fmt_decimal(item.commission, 6),
                item.trade_id,
            ]
            for item in slices
        ]
        fill_table(
            table,
            ["时间（IB账单）", "方向", "角色", "数量", "价格", "成交额USD", "佣金USD", "交易ID"],
            rows or [["--", "--", "--", "--", "--", "--", "--", "暂无成交"]],
        )

    def selected_strategy_id(self) -> str | None:
        row = self.strategy_table.currentRow()
        if row < 0:
            return None
        item = self.strategy_table.item(row, 0)
        value = item.data(Qt.UserRole) if item is not None else None
        return str(value) if value else None

    def _selected_strategy(self) -> engine.VenueClose | None:
        strategy_id = self.selected_strategy_id()
        if self.result is None or not strategy_id:
            return None
        return next((item for item in self.result.venue_closes if item.id == strategy_id), None)

    def _request_mapping(self) -> None:
        strategy_id = self.selected_strategy_id()
        if strategy_id:
            self.mappingRequested.emit(strategy_id)

    def _clear_mapping(self) -> None:
        strategy_id = self.selected_strategy_id()
        if strategy_id:
            self.mappingCleared.emit(strategy_id)

    def _refresh_selected_details(self) -> None:
        close = self._selected_strategy()
        enabled = close is not None
        self.mapping_button.setEnabled(enabled)
        self.clear_mapping_button.setEnabled(bool(close and close.manual_ib_mapping))
        if close is None:
            fill_table(self.domestic_table, ["买入日期", "账户", "买入合同", "使用份额", "FIFO成本"], [["--", "--", "--", "--", "请选择一笔自平"]])
            self._fill_ib_slices(self.ib_open_table, ())
            self._fill_ib_slices(self.ib_close_table, ())
            return
        domestic_rows = [
            [
                item.trade_day.isoformat(),
                item.source,
                item.contract_no,
                f"{item.qty:,}",
                fmt_money(item.cost),
            ]
            for item in close.matches
        ]
        fill_table(
            self.domestic_table,
            ["买入日期", "账户", "买入合同", "使用份额", "FIFO成本"],
            domestic_rows or [["--", "--", "--", "--", "无FIFO来源"]],
        )
        self._fill_ib_slices(self.ib_open_table, close.ib_open)
        self._fill_ib_slices(self.ib_close_table, close.ib_close)

    def update_data(self, result: engine.CalculationResult) -> None:
        selected_id = self.selected_strategy_id()
        self.result = result
        completed = result.completed_strategy_self_closes
        self.metric_values["completed"].setText(
            f"{len(completed):,} 组 / {result.strategy_self_close_qty:,} 股"
        )
        self._set_pnl_metric(
            "domestic",
            result.strategy_self_domestic_pnl_cny,
            f"{fmt_money(result.strategy_self_domestic_pnl_cny)} RMB",
        )
        self._set_pnl_metric(
            "ib",
            result.strategy_self_ib_pnl_cny,
            f"{fmt_money(result.strategy_self_ib_pnl_usd)} USD / {fmt_money(result.strategy_self_ib_pnl_cny)} RMB",
        )
        self._set_pnl_metric(
            "total",
            result.strategy_self_total_cny,
            f"{fmt_money(result.strategy_self_total_cny)} RMB",
        )
        self.metric_values["incomplete"].setText(
            f"待闭合 {result.incomplete_strategy_self_close_count:,} / 残余 {len(result.ib_self_closes):,} / 风险 {len(result.unmatched_ib):,}"
        )

        strategy_rows: list[list[object]] = []
        for close in result.venue_closes:
            opening_dt = min((item.dt for item in close.ib_open), default=None)
            closing_dt = max((item.dt for item in close.ib_close), default=None)
            strategy_rows.append([
                close.status,
                close.trade_day.isoformat(),
                close.source,
                close.contract_no,
                f"{close.qty:,}",
                f"{close.ib_open_qty:,}/{close.hedge_target:,}/{close.ib_close_qty:,}",
                opening_dt.strftime("%Y-%m-%d %H:%M:%S") if opening_dt else "--",
                closing_dt.strftime("%Y-%m-%d %H:%M:%S") if closing_dt else "--",
                self._duration_text(opening_dt, closing_dt) if opening_dt and closing_dt else "--",
                fmt_money(close.cost),
                fmt_money(close.proceeds),
                fmt_money(close.pnl),
                fmt_decimal(self._average_price(close.ib_open), 4) if close.ib_open else "--",
                fmt_decimal(self._average_price(close.ib_close), 4) if close.ib_close else "--",
                fmt_decimal(close.ib_commission_usd, 4),
                fmt_money(close.ib_trade_pnl_usd),
                fmt_money(close.ib_pnl_cny),
                fmt_money(close.total_pnl_cny),
                "人工" if close.manual_ib_mapping else "自动",
            ])
        fill_table(
            self.strategy_table,
            self.STRATEGY_HEADERS,
            strategy_rows or [["--"] * (len(self.STRATEGY_HEADERS) - 1) + ["暂无国内场内碎单自平"]],
            payloads=[item.id for item in result.venue_closes] if strategy_rows else None,
        )
        selected_row = 0
        for row, close in enumerate(result.venue_closes):
            if close.is_complete:
                positive = close.total_pnl_cny >= 0
                background = QColor("#ecfdf3" if positive else "#fef2f2")
                foreground = QColor("#14532d" if positive else "#991b1b")
            else:
                background = QColor("#fffbeb")
                foreground = QColor("#92400e")
            for column in range(self.strategy_table.columnCount()):
                item = self.strategy_table.item(row, column)
                if item is not None:
                    item.setBackground(background)
                    item.setForeground(foreground)
                    if close.warnings:
                        item.setToolTip("\n".join(close.warnings))
            if close.id == selected_id:
                selected_row = row
        if strategy_rows:
            self.strategy_table.selectRow(selected_row)
            self.strategy_table.horizontalScrollBar().setValue(0)
        else:
            self._refresh_selected_details()

        residual_rows = [
            [
                pair.sequence,
                pair.direction,
                pair.opening.dt.strftime("%Y-%m-%d %H:%M:%S"),
                pair.closing.dt.strftime("%Y-%m-%d %H:%M:%S"),
                self._duration_text(pair.opening.dt, pair.closing.dt),
                f"{pair.qty:,}",
                fmt_decimal(pair.opening.price, 4),
                fmt_decimal(pair.closing.price, 4),
                fmt_decimal(pair.commission_usd, 4),
                fmt_money(pair.trade_pnl_usd),
                fmt_money(pair.pnl_cny),
            ]
            for pair in result.ib_self_closes
        ]
        fill_table(
            self.residual_table,
            self.RESIDUAL_HEADERS,
            residual_rows or [["--"] * (len(self.RESIDUAL_HEADERS) - 1) + ["无未归因IB残余配对"]],
        )
        for row in range(len(residual_rows)):
            for column in range(self.residual_table.columnCount()):
                item = self.residual_table.item(row, column)
                if item is not None:
                    item.setBackground(QColor("#f5f3ff"))
                    item.setForeground(QColor("#5b21b6"))

        unmatched_rows = [
            [
                item.dt.strftime("%Y-%m-%d %H:%M:%S"),
                item.side,
                item.qty,
                fmt_decimal(item.price, 4),
                item.role or "--",
                "未闭合，保留为风险头寸",
            ]
            for item in result.unmatched_ib
        ]
        fill_table(
            self.unmatched_table,
            self.UNMATCHED_HEADERS,
            unmatched_rows or [["--", "--", "--", "--", "--", "无未闭合头寸"]],
        )
        if unmatched_rows:
            for row in range(self.unmatched_table.rowCount()):
                for column in range(self.unmatched_table.columnCount()):
                    item = self.unmatched_table.item(row, column)
                    if item is not None:
                        item.setBackground(QColor("#fffbeb"))
                        item.setForeground(QColor("#92400e"))


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
        self._active_request: tuple[date, bool, str] | None = None
        self._pending_request: tuple[date, bool, str] | None = None
        self._prefetch_thread: QThread | None = None
        self._prefetch_worker: PcfPrefetchWorker | None = None
        self._startup_prefetch_checked = False
        self._shutting_down = False

        self.date_edit = QDateEdit()
        self.date_edit.setCalendarPopup(True)
        self.date_edit.setDisplayFormat("yyyy-MM-dd")
        self.date_edit.setDate(self._qdate(normalize_business_day(date.today(), prefer_backward=True)))
        self.prev_day_button = QPushButton("前一日")
        self.today_button = QPushButton("今日")
        self.next_day_button = QPushButton("下一日")
        self.refresh_button = QPushButton("强制刷新")
        self.search_edit = QLineEdit()
        self.search_edit.setFixedWidth(130)
        self.search_edit.setPlaceholderText("代码，如159518")
        self.search_button = QPushButton("搜索PCF")
        self.status_label = QLabel(
            f"已纳入多只 ETF PCF；默认读取 {self.target_code}，其他工具仍固定使用 {self.target_code} PCF。"
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
        self.left_box = QGroupBox("ETF 当日清单")
        left_layout = QVBoxLayout(self.left_box)
        left_layout.addWidget(self.list_table)
        splitter.addWidget(self.left_box)
        splitter.addWidget(detail_tabs)
        self.left_box.setMaximumWidth(340)
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
        controls.addSpacing(12)
        controls.addWidget(QLabel("快速搜索"))
        controls.addWidget(self.search_edit)
        controls.addWidget(self.search_button)
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
        self.search_button.clicked.connect(self.search_fund_code)
        self.search_edit.returnPressed.connect(self.search_fund_code)
        self.list_table.itemSelectionChanged.connect(self.handle_fund_selection_changed)
        self.date_edit.dateChanged.connect(self.handle_date_changed)
        self.show_fx_placeholder("请选择日期后加载汇率")
        self.clear_detail()

    @staticmethod
    def _qdate(value: date) -> QDate:
        return QDate(value.year, value.month, value.day)

    @staticmethod
    def _python_date(value: QDate) -> date:
        return date(value.year(), value.month(), value.day())

    def startup_prefetch_if_needed(self, now: datetime | None = None) -> None:
        if self._startup_prefetch_checked or self._prefetch_thread is not None:
            return
        self._startup_prefetch_checked = True
        current = now or beijing_now()
        trading_day = current.date()
        if trading_day.weekday() >= 5:
            self.status_label.setText("今日非交易日，未自动拉取 PCF。")
            return
        if current.timetz().replace(tzinfo=None) < clock_time(8, 15):
            self.status_label.setText("北京时间 08:15 前，未自动拉取当日 PCF。")
            return
        missing_items = self.store.missing_focus_items(trading_day)
        if not missing_items:
            self.status_label.setText(
                f"{trading_day:%Y-%m-%d} 的全部 ETF PCF 已在本地缓存，未发起自动请求。"
            )
            return
        self._start_prefetch(trading_day, missing_items)

    def _start_prefetch(self, trading_day: date, items: tuple[szse_pcf.PcfListItem, ...]) -> None:
        self.status_label.setText(
            f"正在后台补全 {trading_day:%Y-%m-%d} 的 {len(items)} 份本地缺失 PCF；"
            "上交所与深交所交错拉取，每个交易所间隔至少 8 秒。"
        )
        thread = QThread(self)
        worker = PcfPrefetchWorker(self.cache_root, trading_day, items)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.progress.connect(self._handle_prefetch_progress)
        worker.finished.connect(self._handle_prefetch_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(self._clear_prefetch_worker)
        thread.finished.connect(thread.deleteLater)
        self._prefetch_thread = thread
        self._prefetch_worker = worker
        thread.start()

    def _handle_prefetch_progress(self, result: object) -> None:
        payload = dict(result)
        trading_day = payload["trading_day"]
        key = str(payload["key"])
        position = int(payload["position"])
        total = int(payload["total"])
        state = str(payload["state"])
        if self.current_index is not None and self.current_index.trade_date == trading_day:
            self.populate_index(self.active_fund_key())
        if state == "loading":
            self.status_label.setText(f"后台拉取 {position}/{total}：{key}...")
        elif state == "cached":
            self.status_label.setText(f"后台已缓存 {position}/{total}：{key}")
        else:
            self.status_label.setText(f"后台拉取失败 {position}/{total}：{key}，将保留为未缓存状态。")

    def _handle_prefetch_finished(self, result: object) -> None:
        payload = dict(result)
        trading_day = payload["trading_day"]
        completed = list(payload.get("completed") or [])
        failures = list(payload.get("failures") or [])
        if self.current_index is not None and self.current_index.trade_date == trading_day:
            self.populate_index(self.active_fund_key())
        self.status_label.setText(
            f"{trading_day:%Y-%m-%d} 自动 PCF 预取完成：缓存 {len(completed)} 份"
            + (f"；失败 {len(failures)} 份。" if failures else "。")
        )

    def _clear_prefetch_worker(self) -> None:
        self._prefetch_worker = None
        self._prefetch_thread = None

    def shutdown(self) -> None:
        self._shutting_down = True
        self._pending_request = None
        for worker in (self._worker, self._prefetch_worker):
            cancel = getattr(worker, "cancel", None)
            if callable(cancel):
                cancel()
        for thread in (self._worker_thread, self._prefetch_thread):
            if thread is not None and thread.isRunning():
                thread.requestInterruption()
                thread.quit()

    def suggest_date(self, value: date | None) -> None:
        if value is None or self._loaded_once:
            return
        self.date_edit.setDate(self._qdate(normalize_business_day(value, prefer_backward=True)))

    def ensure_loaded(self) -> None:
        current_day = self._python_date(self.date_edit.date())
        if not self._loaded_once or self.current_index is None or self.current_index.trade_date != current_day:
            self.load_day(selected_code=self.active_fund_key())

    def selected_code(self) -> str | None:
        key = self.selected_key()
        if not key:
            return None
        _exchange, code = szse_pcf.parse_fund_key(key)
        return code or None

    def selected_key(self) -> str | None:
        row = self.list_table.currentRow()
        if row < 0:
            return None
        item = self.list_table.item(row, 0)
        value = item.data(Qt.UserRole) if item is not None else None
        if not value:
            return None
        exchange, code = szse_pcf.parse_fund_key(str(value))
        if not code:
            return None
        return szse_pcf.fund_key(exchange, code)

    def active_fund_key(self) -> str:
        for raw_code in (
            self.selected_key(),
            self.search_edit.text(),
            szse_pcf.fund_key(self.current_detail.item.exchange, self.current_detail.item.fund_code)
            if self.current_detail is not None
            else "",
        ):
            exchange, code = szse_pcf.parse_fund_key(raw_code or "")
            if szse_pcf.is_focus_fund(exchange, code):
                return szse_pcf.fund_key(exchange, code)
        return szse_pcf.fund_key(szse_pcf.EXCHANGE_SZSE, self.target_code)

    def handle_date_changed(self, _value: QDate) -> None:
        if self._loaded_once or self._worker_thread is not None:
            self.load_day(selected_code=self.active_fund_key())

    def shift_day(self, days: int) -> None:
        current = self._python_date(self.date_edit.date())
        self.date_edit.setDate(self._qdate(shift_business_day(current, days)))

    def jump_today(self) -> None:
        today = normalize_business_day(date.today(), prefer_backward=True)
        q_today = self._qdate(today)
        if self.date_edit.date() == q_today:
            self.load_day(selected_code=self.active_fund_key())
            return
        self.date_edit.setDate(q_today)

    def force_refresh(self) -> None:
        self.load_day(force_refresh=True, selected_code=self.selected_key() or self.active_fund_key())

    def load_day(self, force_refresh: bool = False, selected_code: str | None = None) -> None:
        trading_day = self._python_date(self.date_edit.date())
        exchange, fund_code = szse_pcf.parse_fund_key(selected_code or self.target_code)
        if not fund_code:
            exchange, fund_code = szse_pcf.EXCHANGE_SZSE, self.target_code
        selection_key = szse_pcf.fund_key(exchange, fund_code)
        request = (trading_day, force_refresh, selection_key)
        if request == self._active_request or request == self._pending_request:
            return
        if self._worker_thread is not None:
            self._pending_request = request
            name = szse_pcf.display_fund_name(fund_code, exchange=exchange)
            self.clear_detail(f"{trading_day:%Y-%m-%d} {selection_key} {name} 已加入读取队列")
            self.status_label.setText(
                f"{trading_day:%Y-%m-%d} {selection_key} {name} 已加入读取队列，当前请求完成后继续..."
                + ("（强制刷新）" if force_refresh else "")
            )
            return
        self._start_load(request)

    def _start_load(self, request: tuple[date, bool, str]) -> None:
        trading_day, force_refresh, selection_key = request
        exchange, fund_code = szse_pcf.parse_fund_key(selection_key)
        self._active_request = request
        name = szse_pcf.display_fund_name(fund_code, exchange=exchange)
        self.clear_detail(f"正在读取 {trading_day:%Y-%m-%d} {selection_key} {name} 申购赎回清单")
        self.status_label.setText(
            f"正在后台读取 {trading_day:%Y-%m-%d} 的 {selection_key} {name} 申购赎回清单..."
            + ("（强制刷新）" if force_refresh else "")
        )
        thread = QThread(self)
        worker = PcfLoadWorker(self.cache_root, self.fx_csv_path, trading_day, selection_key, force_refresh)
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
        request = (
            payload["trading_day"],
            bool(payload["force_refresh"]),
            szse_pcf.fund_key(
                str(payload.get("exchange") or szse_pcf.EXCHANGE_SZSE),
                str(payload.get("fund_code") or self.target_code),
            ),
        )
        stale = self._pending_request is not None and self._pending_request != request
        self._active_request = None
        self._worker = None
        self._worker_thread = None
        if not stale and not bool(payload.get("cancelled")) and not self._shutting_down:
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
        selected_exchange = str(payload.get("exchange") or szse_pcf.EXCHANGE_SZSE)
        selected_code = szse_pcf.normalize_fund_code(str(payload.get("fund_code") or "")) or self.target_code
        selected_key = szse_pcf.fund_key(selected_exchange, selected_code)
        self.populate_index(selected_key)

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
            name = szse_pcf.display_fund_name(selected_code, exchange=selected_exchange)
            self.clear_detail(
                f"{trading_day:%Y-%m-%d} 暂无 {selected_key} {name} 清单"
                if detail_error
                else f"{trading_day:%Y-%m-%d} 暂无 {selected_key} {name} 清单"
            )
            if detail_error:
                self.status_label.setText(
                    f"{trading_day:%Y-%m-%d} 暂无 {selected_key} {name} 申购赎回清单"
                    if "暂无" in detail_error
                    else f"{trading_day:%Y-%m-%d} 清单读取失败：{detail_error}"
                )
            else:
                self.status_label.setText(f"{trading_day:%Y-%m-%d} 暂无 {selected_key} {name} 申购赎回清单")
            return

        assert isinstance(detail, szse_pcf.PcfDetail)
        self.current_detail = detail
        self.populate_detail(detail)
        code = szse_pcf.normalize_fund_code(detail.item.fund_code)
        key = szse_pcf.fund_key(detail.item.exchange, code)
        name = szse_pcf.display_fund_name(code, detail.fund_name, detail.item.exchange)
        trading_day_text = szse_pcf.display_value("TradingDay", detail.trading_day, detail.item.exchange)
        self.status_label.setText(
            f"已加载 {trading_day_text} 的 {key} {name}；成分 {len(detail.components)} 条"
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

    def _visible_index_items(self, selected_code: str | None = None) -> tuple[szse_pcf.PcfListItem, ...]:
        if self.current_index is None:
            return ()
        selected_exchange, selected_code_value = szse_pcf.parse_fund_key(selected_code or "")
        selected_key = szse_pcf.fund_key(selected_exchange, selected_code_value) if selected_code_value else ""
        rows = list(szse_pcf.focus_fund_items(self.current_index.items))
        selected_item = (
            self.current_index.find(selected_code_value, selected_exchange)
            if selected_code_value
            else None
        )
        if selected_item is not None:
            selected_seen = any(
                szse_pcf.fund_key(item.exchange, item.fund_code) == selected_key
                for item in rows
            )
            if not selected_seen:
                rows.append(selected_item)
        return tuple(rows)

    def populate_index(self, selected_code: str | None = None) -> None:
        if self.current_index is None:
            fill_table(self.list_table, ["代码", "名称"], [])
            self.clear_detail()
            self.left_box.setTitle("ETF 当日清单")
            return
        rows: list[list[object]] = []
        payloads: list[object] = []
        cache_states: list[bool] = []
        selected_exchange, selected_code_value = szse_pcf.parse_fund_key(selected_code or "")
        normalized_selected = (
            szse_pcf.fund_key(selected_exchange, selected_code_value)
            if selected_code_value
            else ""
        )
        for item in self._visible_index_items(normalized_selected):
            code = szse_pcf.normalize_fund_code(item.fund_code)
            key = szse_pcf.fund_key(item.exchange, code)
            rows.append([
                code,
                szse_pcf.display_fund_name(code, item.page_label or item.title, item.exchange),
            ])
            payloads.append(key)
            cache_states.append(self.store.is_fund_detail_cached(item.trade_date, code, item.exchange))
        self.list_table.blockSignals(True)
        fill_table(self.list_table, ["代码", "名称"], rows, payloads=payloads)
        for row, cached in enumerate(cache_states):
            color = QColor("#15803d") if cached else QColor("#64748b")
            tooltip = "本地已缓存 PCF" if cached else "本地尚无 PCF 缓存"
            for column in range(self.list_table.columnCount()):
                table_item = self.list_table.item(row, column)
                if table_item is not None:
                    table_item.setForeground(color)
                    table_item.setToolTip(tooltip)
        self.left_box.setTitle(f"ETF 当日清单（{len(rows)}）")
        if self.list_table.columnCount() >= 1:
            self.list_table.setColumnWidth(0, 84)
        if not rows:
            self.list_table.blockSignals(False)
            self.clear_detail()
            return
        chosen = 0
        for row, payload in enumerate(payloads):
            if payload == normalized_selected:
                chosen = row
                break
        self.list_table.selectRow(chosen)
        self.list_table.blockSignals(False)

    def handle_fund_selection_changed(self) -> None:
        key = self.selected_key()
        if not key:
            return
        exchange, code = szse_pcf.parse_fund_key(key)
        self.search_edit.setText(key)
        trading_day = self._python_date(self.date_edit.date())
        current_code = (
            szse_pcf.normalize_fund_code(self.current_detail.item.fund_code)
            if self.current_detail is not None
            else ""
        )
        current_exchange = (
            szse_pcf.normalize_exchange(self.current_detail.item.exchange)
            if self.current_detail is not None
            else ""
        )
        if (
            self.current_detail is not None
            and self.current_detail.item.trade_date == trading_day
            and current_code == code
            and current_exchange == szse_pcf.normalize_exchange(exchange)
        ):
            return
        self.load_day(selected_code=key)

    def search_fund_code(self) -> None:
        exchange, code = szse_pcf.parse_fund_key(self.search_edit.text())
        if not code:
            self.status_label.setText("请输入 6 位基金代码后再搜索。")
            return
        key = szse_pcf.fund_key(exchange, code)
        self.search_edit.setText(key)
        if not szse_pcf.is_focus_fund(exchange, code):
            self.status_label.setText(f"{key} 不在已纳入的 1navs SZ/SH ETF PCF 列表中，未发起交易所请求。")
            return
        if self.current_index is not None and self.current_index.find(code, exchange) is not None:
            self.populate_index(key)
        self.load_day(selected_code=key)

    def clear_detail(self, message: str | None = None) -> None:
        clear_layout(self.summary_grid)
        text = message or f"请选择日期后读取 {self.target_code} 清单"
        self.summary_group.setTitle("清单概要")
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

    @staticmethod
    def _pcf_metadata_day(metadata: dict[str, str], field: str) -> date | None:
        text = str(metadata.get(field) or "").strip()
        if not text:
            return None
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
        if text.isdigit() and len(text) == 8:
            try:
                return datetime.strptime(text, "%Y%m%d").date()
            except ValueError:
                return None
        return None

    def actual_cash_component_from_cached_future_pcf(
        self,
        detail: szse_pcf.PcfDetail,
    ) -> tuple[date, str] | None:
        """Find the first later cached PCF whose PreTradingDay is this PCF's TradingDay.

        A later PCF's CashComponent is the known cash difference for the
        selected PCF trading day.  This is intentionally cache-only so merely
        viewing an old day never starts a network request or bypasses PCF rate
        limits.
        """
        target_day = self._pcf_metadata_day(detail.metadata, "TradingDay") or detail.item.trade_date
        exchange = szse_pcf.normalize_exchange(detail.item.exchange)
        code = szse_pcf.normalize_fund_code(detail.item.fund_code)
        if not self.cache_root.exists() or not code:
            return None
        candidate_days: list[date] = []
        for path in self.cache_root.iterdir():
            if not path.is_dir():
                continue
            try:
                candidate_day = date.fromisoformat(path.name)
            except ValueError:
                continue
            if candidate_day > target_day:
                candidate_days.append(candidate_day)
        for candidate_day in sorted(candidate_days):
            if not self.store.is_fund_detail_cached(candidate_day, code, exchange):
                continue
            try:
                future_detail = self.store.ensure_fund_detail(
                    candidate_day,
                    code,
                    exchange=exchange,
                )
            except Exception:
                # A partially written cache should behave like a missing future
                # PCF and must not make the selected PCF page fail to render.
                continue
            if self._pcf_metadata_day(future_detail.metadata, "PreTradingDay") != target_day:
                continue
            raw_cash_component = str(future_detail.metadata.get("CashComponent") or "").strip()
            if raw_cash_component:
                return candidate_day, raw_cash_component
        return None

    def populate_detail(self, detail: szse_pcf.PcfDetail) -> None:
        clear_layout(self.summary_grid)
        code = szse_pcf.normalize_fund_code(detail.item.fund_code)
        exchange = szse_pcf.normalize_exchange(detail.item.exchange)
        key = szse_pcf.fund_key(exchange, code)
        self.summary_group.setTitle(
            f"{key} {szse_pcf.display_fund_name(code, detail.fund_name, exchange)} 清单概要"
        )

        fields: list[tuple[str, str, bool]] = []
        summary_order = (
            szse_pcf.SSE_SUMMARY_FIELD_ORDER
            if exchange == szse_pcf.EXCHANGE_SSE
            else szse_pcf.SUMMARY_FIELD_ORDER
        )
        highlight_fields = (
            {"EstimatedCashComponent", "PreCashComponent", "NAVperCU", "NAV", "RedemptionLimit"}
            if exchange == szse_pcf.EXCHANGE_SSE
            else PCF_SUMMARY_HIGHLIGHT_FIELDS
        )
        target_day = self._pcf_metadata_day(detail.metadata, "TradingDay") or detail.item.trade_date
        later_actual_cash = self.actual_cash_component_from_cached_future_pcf(detail)
        actual_cash_label = f"当日实际现金差额 · {target_day:%Y-%m-%d}"
        actual_cash_value = ""
        if later_actual_cash is not None:
            source_day, raw_value = later_actual_cash
            actual_cash_value = (
                f"{szse_pcf.display_value('CashComponent', raw_value, exchange)}\n"
                f"来自 {source_day:%Y-%m-%d} PCF"
            )
        for field in summary_order:
            if field in PCF_SUMMARY_HIDDEN_FIELDS:
                continue
            value = detail.metadata.get(field) or ""
            if value:
                label = szse_pcf.display_summary_label(field, exchange)
                reference_day_text = pcf_field_reference_day_text(detail.metadata, field, detail.item.trade_date)
                if field in highlight_fields:
                    label = f"{label} · {reference_day_text}"
                fields.append(
                    (
                        label,
                        szse_pcf.display_value(field, value, exchange),
                        field in highlight_fields,
                    )
                )
                if field in {"EstimateCashComponent", "EstimatedCashComponent"}:
                    fields.append((actual_cash_label, actual_cash_value, later_actual_cash is not None))
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
                for column in szse_pcf.component_columns(detail.components, exchange)
                if column not in PCF_COMPONENT_HIDDEN_FIELDS
            ]
            headers = [szse_pcf.display_component_label(column, exchange) for column in columns]
            rows = [
                [szse_pcf.display_value(column, component.get(column, ""), exchange) for column in columns]
                for component in detail.components
            ]
            fill_table(self.component_table, headers, rows)
        else:
            fill_table(self.component_table, ["提示"], [["当前条目暂无结构化 XML 成分数据"]])
        self.raw_text.setPlainText(detail.raw_text or "当前条目暂无原始 TXT 内容。")


class ArrivalCalibrationTab(QWidget):
    calibrationChanged = pyqtSignal()

    def __init__(self, config: dict[str, object], pcf_tab: SzsePcfTab, parent=None) -> None:
        super().__init__(parent)
        self.pcf_tab = pcf_tab
        self.result: engine.CalculationResult | None = None
        self.fx_store = fx_rates.FxRateStore(Path(str(config["fx_rates_csv_path"])))
        self.price_provider = market_data.CsvXopPriceProvider(Path(str(config["xop_price_csv_path"])))
        self.calibration_store = basket_calibration.CalibrationStore(Path(str(config["calibration_csv_path"])))
        self.observation_store = basket_calibration.SettlementObservationStore(
            Path(str(config["settlement_observation_csv_path"]))
        )
        self.price_window = str(config.get("estimate_price_window") or "1540_1600")
        self.tws_host = str(config.get("tws_host") or "127.0.0.1")
        self.tws_port = int(config.get("tws_port") or 7496)
        self.tws_client_id = int(config.get("tws_client_id") or 8888)
        self._date_initialized = False
        self.status_label = QLabel(
            "请在上方四个功能页中选择一项任务；每页只处理一类计算。"
        )
        self.status_label.setObjectName("sourceHint")
        self.status_label.setWordWrap(True)
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        initial_day = normalize_business_day(date.today(), prefer_backward=True)

        def date_edit() -> QDateEdit:
            widget = QDateEdit()
            widget.setCalendarPopup(True)
            widget.setDisplayFormat("yyyy-MM-dd")
            widget.setDate(QDate(initial_day.year, initial_day.month, initial_day.day))
            return widget

        def quantity_spin() -> QSpinBox:
            widget = QSpinBox()
            widget.setRange(1, 2_000_000_000)
            widget.setSingleStep(1_000_000)
            widget.setValue(1_000_000)
            widget.setSuffix(" 份")
            return widget

        def guide(text: str) -> QLabel:
            widget = QLabel(text)
            widget.setObjectName("calculationGuide")
            widget.setWordWrap(True)
            widget.setTextInteractionFlags(Qt.TextSelectableByMouse)
            return widget

        # Page 1: estimate any date without requiring a QMT redemption record.
        self.query_date = date_edit()
        self.query_qty = quantity_spin()
        self.manual_shares_check = QCheckBox("手动指定每申赎单位XOP股数")
        self.manual_shares_spin = QDoubleSpinBox()
        self.manual_shares_spin.setDecimals(4)
        self.manual_shares_spin.setRange(1.0, 5000.0)
        self.manual_shares_spin.setSingleStep(1.0)
        self.manual_shares_spin.setValue(998.0)
        self.manual_shares_spin.setSuffix(" 股")
        self.manual_shares_spin.setEnabled(False)
        self.calculate_date_button = QPushButton("计算指定日期的预估到账金额")
        date_controls = QHBoxLayout()
        date_controls.addWidget(QLabel("国内发起赎回的交易日"))
        date_controls.addWidget(self.query_date)
        date_controls.addWidget(QLabel("假设赎回份额"))
        date_controls.addWidget(self.query_qty)
        date_controls.addWidget(self.manual_shares_check)
        date_controls.addWidget(self.manual_shares_spin)
        date_controls.addWidget(self.calculate_date_button)
        date_controls.addStretch(1)
        self.date_estimate_table = configured_table()
        date_page = QWidget()
        date_layout = QVBoxLayout(date_page)
        date_layout.addWidget(guide(
            "用途：在没有 QMT 赎回记录时，预估指定交易日和份额的到账金额。\n"
            "使用：选择国内赎回交易日，输入假设赎回份额，然后点击计算。"
            "默认采用PCF扣除现金差额后反推的XOP股数；勾选“手动指定”后，可仅对本次预估覆盖该股数，不会改写PCF校准记录。\n"
            "计算：预估XOP股数=采用的每申赎单位XOP股数×赎回份额÷最小申赎单位；"
            "预估补券退款=预估XOP股数×XOP卖出价格×赎回日CFETS美元人民币收盘价；"
            "预估总回款=预估补券退款+PCF预估现金差额。"
        ))
        date_layout.addLayout(date_controls)
        date_layout.addWidget(self.date_estimate_table, 1)

        # Page 2: estimates for baskets already present in QMT.
        self.refresh_button = QPushButton("刷新当前QMT篮子预估")
        self.estimate_table = configured_table()
        qmt_page = QWidget()
        qmt_layout = QVBoxLayout(qmt_page)
        qmt_layout.addWidget(guide(
            "用途：查看当前 QMT 交割单中每个已发起赎回篮子的预估和实际结果。\n"
            "计算：与“指定日期到账预估”相同；如 QMT 已出现实际现金差额，现金差额优先使用 QMT 实际值；"
            "如已出现实际补券退款，同时显示实际值和预估值以便比较。"
        ))
        qmt_controls = QHBoxLayout()
        qmt_controls.addWidget(self.refresh_button)
        qmt_controls.addStretch(1)
        qmt_layout.addLayout(qmt_controls)
        qmt_layout.addWidget(self.estimate_table, 1)

        # Page 3: build and persist PCF calibration points.
        self.calibration_date = date_edit()
        self.save_date_point_button = QPushButton("计算并保存该日PCF股数校准")
        self.write_pcf_button = QPushButton("保存“申购赎回清单”页当前日期")
        self.pcf_preview_table = configured_table()
        self.pcf_table = configured_table()
        pcf_page = QWidget()
        pcf_layout = QVBoxLayout(pcf_page)
        pcf_layout.addWidget(guide(
            "用途：根据当日 PCF 反推每一个最小申赎单位等效的 XOP 股数，并保存给到账预估和实际到账反校准使用。\n"
            "按资产净值反推股数 = 最小申赎单位资产净值 ÷ PCF估值日SAFE中间价 ÷ XOP估值日收盘价。\n"
            "扣除现金差额后反推股数 = (最小申赎单位资产净值 - PCF现金差额) ÷ SAFE中间价 ÷ XOP收盘价。\n"
            "程序默认采用“扣除现金差额后反推股数”；该数值不会代入篮子汇总、篮子配对或 IB 盈亏归因，主账务计算始终固定为每申赎单位 990 股。"
        ))
        pcf_controls = QHBoxLayout()
        pcf_controls.addWidget(QLabel("PCF交易日"))
        pcf_controls.addWidget(self.calibration_date)
        pcf_controls.addWidget(self.save_date_point_button)
        pcf_controls.addWidget(self.write_pcf_button)
        pcf_controls.addStretch(1)
        pcf_layout.addLayout(pcf_controls)
        pcf_splitter = QSplitter(Qt.Vertical)
        preview_box = QGroupBox("本次校准计算明细")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.addWidget(self.pcf_preview_table)
        stored_box = QGroupBox("已保存的PCF股数校准点")
        stored_layout = QVBoxLayout(stored_box)
        stored_layout.addWidget(self.pcf_table)
        pcf_splitter.addWidget(preview_box)
        pcf_splitter.addWidget(stored_box)
        pcf_layout.addWidget(pcf_splitter, 1)

        # Page 4: validate another redemption's actual receipts and QMT history.
        self.actual_query_date = date_edit()
        self.actual_query_qty = quantity_spin()
        self.external_refund = QDoubleSpinBox()
        self.external_refund.setDecimals(2)
        self.external_refund.setRange(0, 10_000_000_000)
        self.external_refund.setSuffix(" 元")
        self.external_cash_difference = QDoubleSpinBox()
        self.external_cash_difference.setDecimals(2)
        self.external_cash_difference.setRange(-10_000_000_000, 10_000_000_000)
        self.external_cash_difference.setSuffix(" 元")
        self.calculate_external_button = QPushButton("用外部实际到账数据反推XOP股数")
        self.write_observations_button = QPushButton("从当前QMT写入已到账历史反校准")
        self.actual_validation_table = configured_table()
        self.observation_table = configured_table()
        actual_page = QWidget()
        actual_layout = QVBoxLayout(actual_page)
        actual_layout.addWidget(guide(
            "用途：把别人或本账户的实际补券退款与本程序的 PCF 校准股数进行对照。\n"
            "使用：输入赎回交易日、赎回份额、交割单中的“ETF申购退款”和“ETF现金差额”。补券退款不能填两项之和。\n"
            "反推每申赎单位XOP股数 = 实际ETF申购退款 ÷ 赎回日CFETS收盘价 ÷ XOP卖出价格代理 ÷ (赎回份额÷最小申赎单位)。\n"
            "反推误差 = 外部实际到账反推股数 - PCF扣除现金差额后反推股数。"
        ))
        actual_controls = QGridLayout()
        actual_controls.addWidget(QLabel("赎回交易日"), 0, 0)
        actual_controls.addWidget(self.actual_query_date, 0, 1)
        actual_controls.addWidget(QLabel("实际赎回份额"), 0, 2)
        actual_controls.addWidget(self.actual_query_qty, 0, 3)
        actual_controls.addWidget(QLabel("交割单中的ETF申购退款"), 1, 0)
        actual_controls.addWidget(self.external_refund, 1, 1)
        actual_controls.addWidget(QLabel("交割单中的ETF现金差额"), 1, 2)
        actual_controls.addWidget(self.external_cash_difference, 1, 3)
        actual_controls.addWidget(self.calculate_external_button, 0, 4, 2, 1)
        actual_controls.addWidget(self.write_observations_button, 0, 5, 2, 1)
        actual_layout.addLayout(actual_controls)
        actual_splitter = QSplitter(Qt.Vertical)
        validation_box = QGroupBox("单笔外部到账反推明细")
        validation_layout = QVBoxLayout(validation_box)
        validation_layout.addWidget(self.actual_validation_table)
        history_box = QGroupBox("已保存的历史到账反校准")
        history_layout = QVBoxLayout(history_box)
        history_layout.addWidget(self.observation_table)
        actual_splitter.addWidget(validation_box)
        actual_splitter.addWidget(history_box)
        actual_layout.addWidget(actual_splitter, 1)

        self.functional_tabs = QTabWidget()
        self.functional_tabs.addTab(date_page, "指定日期到账预估")
        self.functional_tabs.addTab(qmt_page, "当前QMT篮子预估")
        self.functional_tabs.addTab(pcf_page, "PCF隐含XOP股数校准")
        self.functional_tabs.addTab(actual_page, "实际到账反校准")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.addWidget(self.functional_tabs, 1)
        layout.addWidget(self.status_label)

        self.refresh_button.clicked.connect(self.refresh_data)
        self.write_pcf_button.clicked.connect(self.write_pcf_point)
        self.write_observations_button.clicked.connect(self.write_observations)
        self.calculate_date_button.clicked.connect(lambda: self.calculate_selected_date(save_point=False))
        self.manual_shares_check.toggled.connect(self.manual_shares_spin.setEnabled)
        self.save_date_point_button.clicked.connect(self.save_calibration_date)
        self.calculate_external_button.clicked.connect(self.calculate_external_validation)
        fill_table(self.date_estimate_table, ["状态/提示"], [["选择日期和份额后，点击“计算指定日期的预估到账金额”。"]])
        fill_table(self.pcf_preview_table, ["状态/提示"], [["选择PCF交易日后，点击“计算并保存”。"]])
        fill_table(self.actual_validation_table, ["状态/提示"], [["输入外部实际到账数据后点击反推。"]])
        self.refresh_data()

    @staticmethod
    def _metadata_day(value: str) -> date:
        text = str(value or "").strip()
        if len(text) == 8 and text.isdigit():
            return date(int(text[:4]), int(text[4:6]), int(text[6:8]))
        return date.fromisoformat(text)

    def update_data(self, result: engine.CalculationResult) -> None:
        self.result = result
        if not self._date_initialized:
            suggested = normalize_business_day(result.qmt_latest_day or date.today(), prefer_backward=True)
            value = QDate(suggested.year, suggested.month, suggested.day)
            self.query_date.setDate(value)
            self.calibration_date.setDate(value)
            self.actual_query_date.setDate(value)
            self._date_initialized = True
        self.refresh_data()

    def selected_query_day(self) -> date:
        value = self.query_date.date()
        return date(value.year(), value.month(), value.day())

    @staticmethod
    def date_edit_value(widget: QDateEdit) -> date:
        value = widget.date()
        return date(value.year(), value.month(), value.day())

    def ensure_xop_close(self, trade_day: date) -> None:
        try:
            self.price_provider.get_close(trade_day)
            return
        except KeyError:
            pass
        prices = backfill_xop_from_tws.fetch_prices(
            trade_day,
            trade_day,
            host=self.tws_host,
            port=self.tws_port,
            client_id=self.tws_client_id,
            intraday_days=set(),
        )
        market_data.upsert_xop_prices(self.price_provider.csv_path, prices)

    def ensure_xop_data(self, valuation_day: date, redeem_day: date) -> None:
        missing = False
        try:
            self.price_provider.get_close(valuation_day)
            self.price_provider.get_window_price(redeem_day, self.price_window)
        except KeyError:
            missing = True
        if not missing:
            return
        prices = backfill_xop_from_tws.fetch_prices(
            min(valuation_day, redeem_day),
            max(valuation_day, redeem_day),
            host=self.tws_host,
            port=self.tws_port,
            client_id=self.tws_client_id,
            intraday_days={redeem_day},
        )
        market_data.upsert_xop_prices(self.price_provider.csv_path, prices)

    def point_for_query_day(self, trade_day: date) -> basket_calibration.PcfCalibrationPoint:
        stored = self.calibration_store.point_for_day(trade_day)
        if stored is not None:
            return stored
        detail = self.pcf_tab.store.ensure_target_detail(trade_day)
        valuation_day = self._metadata_day(detail.metadata.get("PreTradingDay") or "")
        safe_mid = self.fx_store.get_usd_cny_safe_mid(valuation_day)
        if safe_mid is None:
            self.fx_store.ensure_trade_date(valuation_day)
            safe_mid = self.fx_store.get_usd_cny_safe_mid(valuation_day)
        if safe_mid is None:
            raise ValueError(f"缺少 {valuation_day:%Y-%m-%d} SAFE 中间价")
        self.ensure_xop_close(valuation_day)
        return basket_calibration.build_pcf_calibration_point(
            detail,
            safe_mid,
            self.price_provider.get_close(valuation_day),
        )

    def estimate_for_date(
        self,
        trade_day: date,
        redeem_qty: int,
        *,
        actual_refund: Decimal | None = None,
        actual_cash_difference: Decimal | None = None,
        manual_shares_per_cu: Decimal | None = None,
    ) -> tuple[
        basket_calibration.PcfCalibrationPoint,
        basket_calibration.BasketCalibrationState,
        settlement_estimator.DateRedemptionEstimate,
    ]:
        point = self.point_for_query_day(trade_day)
        self.ensure_xop_data(point.valuation_day, trade_day)
        settlement_fx = self.fx_store.get_usd_cny_cfets_close(trade_day)
        if settlement_fx is None:
            self.fx_store.ensure_trade_date(trade_day)
            settlement_fx = self.fx_store.get_usd_cny_cfets_close(trade_day)
        if settlement_fx is None:
            raise ValueError(f"缺少 {trade_day:%Y-%m-%d} CFETS 美元人民币收盘价")
        shares_per_cu = manual_shares_per_cu if manual_shares_per_cu is not None else point.chosen_q
        if shares_per_cu <= 0:
            raise ValueError("手动指定的每申赎单位XOP股数必须大于 0")
        state = basket_calibration.BasketCalibrationState(
            trade_day=trade_day,
            shares_per_cu=shares_per_cu,
            method="manual_override" if manual_shares_per_cu is not None else point.chosen_method,
            confidence="manual" if manual_shares_per_cu is not None else basket_calibration.calibration_confidence(point),
            sample_count=1,
            warning=point.warning,
        )
        estimate = settlement_estimator.estimate_redemption_for_date(
            trade_day,
            redeem_qty,
            state,
            point,
            self.price_provider.get_window_price(trade_day, self.price_window),
            settlement_fx,
            actual_refund_cny=actual_refund,
            actual_cash_difference_cny=actual_cash_difference,
            price_window=self.price_window,
        )
        return point, state, estimate

    def calculate_selected_date(self, *, save_point: bool = False) -> None:
        trade_day = self.selected_query_day()
        try:
            manual_shares = (
                Decimal(str(self.manual_shares_spin.value())) if self.manual_shares_check.isChecked() else None
            )
            point, state, estimate = self.estimate_for_date(
                trade_day,
                self.query_qty.value(),
                manual_shares_per_cu=manual_shares,
            )
        except Exception as exc:
            self.status_label.setText(f"{trade_day:%Y-%m-%d} 到账预估失败：{exc}")
            QMessageBox.critical(self, "指定日期到账预估失败", str(exc))
            return
        rows = [
            ["国内发起赎回的交易日", trade_day.isoformat(), "用户选择的 T 日"],
            ["假设赎回份额", f"{estimate.redeem_qty:,} 份", "不要求 QMT 中存在该笔赎回"],
            ["PCF最小申赎单位", f"{estimate.creation_redemption_unit:,} 份", "来自 PCF CreationRedemptionUnit"],
            ["PCF资产估值日", point.valuation_day.isoformat(), "来自 PCF PreTradingDay"],
            ["PCF最小申赎单位资产净值", fmt_money(point.nav_per_cu), "来自 PCF NAVperCU"],
            ["PCF现金差额", fmt_money(point.cash_component), "来自 PCF CashComponent"],
            ["PCF估值日SAFE美元人民币中间价", fmt_decimal(point.safe_mid_fx, 6), "只用于反推PCF隐含股数"],
            ["XOP在PCF估值日的收盘价", fmt_decimal(point.xop_close, 4), "只用于反推PCF隐含股数"],
            ["PCF按资产净值反推的每申赎单位XOP股数", fmt_decimal(point.q_nav, 4), "资产净值 ÷ SAFE中间价 ÷ XOP收盘价"],
            ["PCF扣除现金差额后反推的每申赎单位XOP股数", fmt_decimal(point.q_net, 4), "(资产净值-现金差额) ÷ SAFE中间价 ÷ XOP收盘价"],
            [
                "本次到账预估采用的每申赎单位XOP股数",
                fmt_decimal(state.shares_per_cu, 4),
                "用户手动指定；仅影响本次预估，不修改PCF校准记录"
                if manual_shares is not None
                else "默认采用PCF扣除现金差额后的反推股数",
            ],
            ["本次赎回预估对应的XOP股数", fmt_decimal(estimate.estimated_xop_shares, 4), "采用股数 × 赎回份额 ÷ 最小申赎单位"],
            ["XOP预估卖出价格", fmt_decimal(estimate.xop_price, 4), price_window_text(estimate.price_window)],
            ["赎回日CFETS美元人民币收盘价", fmt_decimal(estimate.settlement_fx, 6), "用于把XOP卖出所得美元折算为人民币"],
            ["预估ETF申购退款（补券退款）", fmt_money(estimate.estimated_refund_cny), "预估XOP股数 × XOP卖出价 × CFETS收盘价"],
            ["预估ETF现金差额", fmt_money(estimate.estimated_cash_difference_cny), "PCF EstimateCashComponent × 赎回份额比例"],
            ["预估国内总回款", fmt_money(estimate.estimated_total_cash_cny), "预估ETF申购退款 + 预估ETF现金差额"],
            [
                "校准置信度与提示",
                "手动指定（不进行PCF置信度评级）"
                if manual_shares is not None
                else confidence_text(state.confidence) + (f"；{point.warning}" if point.warning else ""),
                "手动指定模式只计算金额；自动模式中，高=股数在990–1000且无警告，中=在980–1010但有警告",
            ],
        ]
        fill_explanation_table(self.date_estimate_table, ["计算项目", "计算结果", "数据来源或计算方法"], rows)
        self.status_label.setText(
            f"已计算 {trade_day:%Y-%m-%d}"
            + (f"（手动指定每申赎单位 {manual_shares:.4f} 股XOP）" if manual_shares is not None else "")
            + f"：预估ETF申购退款 {estimate.estimated_refund_cny:,.2f} 元，"
            f"预估总回款 {estimate.estimated_total_cash_cny:,.2f} 元"
        )

    def save_calibration_date(self) -> None:
        trade_day = self.date_edit_value(self.calibration_date)
        try:
            point = self.point_for_query_day(trade_day)
            self.calibration_store.append_or_replace_pcf_point(point)
        except Exception as exc:
            QMessageBox.critical(self, "PCF股数校准失败", str(exc))
            return
        rows = [
            ["PCF交易日", point.pcf_trading_day.isoformat(), "来自 PCF TradingDay"],
            ["PCF资产估值日", point.valuation_day.isoformat(), "来自 PCF PreTradingDay"],
            ["PCF最小申赎单位", f"{point.creation_redemption_unit:,} 份", "来自 CreationRedemptionUnit"],
            ["PCF最小申赎单位资产净值", fmt_money(point.nav_per_cu), "来自 NAVperCU"],
            ["PCF现金差额", fmt_money(point.cash_component), "来自 CashComponent"],
            ["SAFE美元人民币中间价", fmt_decimal(point.safe_mid_fx, 6), f"{point.valuation_day:%Y-%m-%d} 的SAFE中间价"],
            ["XOP估值日收盘价", fmt_decimal(point.xop_close, 4), f"{point.valuation_day:%Y-%m-%d} 的XOP收盘价"],
            ["按资产净值反推的每申赎单位XOP股数", fmt_decimal(point.q_nav, 4), "未扣除PCF现金差额，仅作对照"],
            ["扣除现金差额后反推的每申赎单位XOP股数", fmt_decimal(point.q_net, 4), "当前程序采用该值"],
            ["校准置信度与提示", confidence_text(basket_calibration.calibration_confidence(point)) + (f"；{point.warning}" if point.warning else ""), "用于判断反推股数是否在合理区间"],
        ]
        fill_explanation_table(self.pcf_preview_table, ["校准项目", "校准结果", "数据来源或计算方法"], rows)
        self.populate_pcf_points()
        self.status_label.setText(
            f"已保存 {trade_day:%Y-%m-%d} PCF股数校准；采用的每申赎单位XOP股数 {point.chosen_q:.4f}"
        )
        self.calibrationChanged.emit()

    def calculate_external_validation(self) -> None:
        trade_day = self.date_edit_value(self.actual_query_date)
        if self.external_refund.value() <= 0:
            QMessageBox.information(self, "缺少实际退款", "请输入交割单中的“ETF申购退款”，不要加上ETF现金差额。")
            return
        try:
            point, _state, estimate = self.estimate_for_date(
                trade_day,
                self.actual_query_qty.value(),
                actual_refund=Decimal(str(self.external_refund.value())),
                actual_cash_difference=Decimal(str(self.external_cash_difference.value())),
            )
        except Exception as exc:
            QMessageBox.critical(self, "实际到账反校准失败", str(exc))
            return
        inferred = estimate.inferred_shares_per_cu
        error = estimate.error_vs_calibration
        rows = [
            ["赎回交易日", trade_day.isoformat(), "外部赎回数据对应的 T 日"],
            ["实际赎回份额", f"{estimate.redeem_qty:,} 份", "用于换算最小申赎单位比例"],
            ["交割单中的ETF申购退款", fmt_money(estimate.actual_refund_cny or Decimal("0")), "只填补券退款，不包含ETF现金差额"],
            ["交割单中的ETF现金差额", fmt_money(estimate.actual_cash_difference_cny or Decimal("0")), "该金额不参与XOP股数反推"],
            ["实际国内总回款", fmt_money(estimate.actual_total_cash_cny or Decimal("0")), "ETF申购退款 + ETF现金差额"],
            ["XOP卖出价格代理", fmt_decimal(estimate.xop_price, 4), price_window_text(estimate.price_window)],
            ["赎回日CFETS美元人民币收盘价", fmt_decimal(estimate.settlement_fx, 6), "用于把实际人民币退款还原为美元"],
            ["根据实际ETF申购退款反推的每申赎单位XOP股数", fmt_decimal(inferred or Decimal("0"), 4), "实际退款 ÷ CFETS收盘价 ÷ XOP卖出价 ÷ 赎回份额比例"],
            ["PCF扣除现金差额后反推的每申赎单位XOP股数", fmt_decimal(point.q_net, 4), "当日PCF校准对照值"],
            ["实际到账反推股数与PCF校准股数的差异", fmt_decimal(error or Decimal("0"), 4), "实际到账反推股数 - PCF校准股数；越接近0越吻合"],
            ["本程序预估ETF申购退款", fmt_money(estimate.estimated_refund_cny), "便于与实际ETF申购退款直接对比"],
        ]
        fill_explanation_table(self.actual_validation_table, ["反校准项目", "反校准结果", "数据来源或计算方法"], rows)
        self.status_label.setText(
            f"已反推 {trade_day:%Y-%m-%d} 实际到账：每申赎单位XOP股数 {inferred:.4f}，"
            f"相对PCF校准值差异 {error:.4f} 股"
        )

    def refresh_data(self) -> None:
        self.populate_pcf_points()
        self.populate_estimates()
        self.populate_observations()

    def populate_pcf_points(self) -> None:
        rows: list[list[object]] = []
        try:
            points = self.calibration_store.load_pcf_points()
            for point in points:
                rows.append(
                    [
                        point.pcf_trading_day.isoformat(),
                        point.valuation_day.isoformat(),
                        fmt_money(point.nav_per_cu),
                        fmt_money(point.cash_component),
                        fmt_decimal(point.safe_mid_fx, 6),
                        fmt_decimal(point.xop_close, 4),
                        fmt_decimal(point.q_nav, 4),
                        fmt_decimal(point.q_net, 4),
                        fmt_decimal(point.chosen_q, 4),
                        calibration_method_text(point.chosen_method),
                        f"{confidence_text(basket_calibration.calibration_confidence(point))}"
                        + (f"；{point.warning}" if point.warning else ""),
                    ]
                )
        except Exception as exc:
            rows = [["--"] * 10 + [f"读取失败：{exc}"]]
        fill_table(
            self.pcf_table,
            [
                "PCF交易日", "PCF资产估值日", "最小申赎单位资产净值", "PCF现金差额",
                "SAFE美元人民币中间价", "XOP估值日收盘价", "按资产净值反推股数",
                "扣除现金差额后反推股数", "程序采用的每申赎单位XOP股数", "采用方法", "校准置信度与提示",
            ],
            rows or [["--"] * 10 + ["暂无校准点"]],
        )

    def populate_estimates(self) -> None:
        rows: list[list[object]] = []
        if self.result is None:
            fill_table(self.estimate_table, ["状态/提示"], [["暂无篮子计算结果"]])
            return
        for basket in self.result.baskets:
            try:
                point = self.calibration_store.latest_point_for_day(basket.redeem_day)
                state = self.calibration_store.latest_state_for_day(basket.redeem_day)
                if point is None or state is None:
                    raise ValueError("缺少不晚于赎回日的 PCF 校准点")
                settlement_fx = self.fx_store.get_usd_cny_cfets_close(basket.redeem_day)
                if settlement_fx is None:
                    raise ValueError("缺少赎回日 CFETS 收盘价")
                xop_price = self.price_provider.get_window_price(basket.redeem_day, self.price_window)
                estimate = settlement_estimator.estimate_redemption(
                    basket, state, point, xop_price, settlement_fx, self.price_window
                )
                refund_display = fmt_money(estimate.estimated_refund_cny)
                if basket.refund_amount > 0:
                    source = "人工" if basket.manual_refund_applied else "实际"
                    refund_display = (
                        f"{fmt_money(basket.refund_amount)} （{source}；估 {fmt_money(estimate.estimated_refund_cny)}）"
                    )
                rows.append(
                    [
                        basket.sequence,
                        basket.redeem_day.isoformat(),
                        basket.contract_no,
                        f"{basket.redeem_qty:,}",
                        fmt_money(estimate.domestic_cost_cny),
                        fmt_decimal(estimate.estimated_xop_shares, 4),
                        price_window_text(estimate.price_window),
                        fmt_decimal(estimate.xop_price, 4),
                        fmt_decimal(estimate.settlement_fx, 6),
                        refund_display,
                        fmt_money(estimate.estimated_cash_difference_cny),
                        fmt_money(estimate.estimated_total_cash_cny),
                        fmt_money(estimate.estimated_domestic_pnl_cny),
                        confidence_text(estimate.confidence) + (f"；{'；'.join(estimate.warnings)}" if estimate.warnings else ""),
                    ]
                )
            except Exception as exc:
                rows.append(
                    [
                        basket.sequence, basket.redeem_day.isoformat(), basket.contract_no,
                        f"{basket.redeem_qty:,}", fmt_money(basket.domestic_cost), *(["--"] * 8), str(exc),
                    ]
                )
        fill_table(
            self.estimate_table,
            [
                "赎回篮子序号", "国内发起赎回的交易日", "QMT合同编号", "实际赎回份额", "国内FIFO买入成本",
                "预估对应的XOP股数", "XOP卖出价格窗口", "XOP预估卖出价格", "CFETS美元人民币收盘价",
                "ETF申购退款（实际值优先显示）", "ETF现金差额", "预估国内总回款",
                "预估国内赎回盈亏", "校准置信度与提示",
            ],
            rows or [["--"] * 13 + ["暂无篮子"]],
        )

    def populate_observations(self) -> None:
        try:
            observations = self.observation_store.load()
            rows = [
                [
                    item.basket_id[:8], item.redeem_day.isoformat(), item.contract_no,
                    fmt_money(item.actual_refund_cny), fmt_money(item.actual_cash_difference_cny),
                    fmt_decimal(item.settlement_fx, 6), fmt_decimal(item.xop_price_proxy, 4),
                    fmt_decimal(item.inferred_shares_per_cu, 4), fmt_decimal(item.pcf_q_net, 4),
                    fmt_decimal(item.error_vs_q_net, 4), "是" if item.included else "否", item.warning,
                ]
                for item in observations
            ]
        except Exception as exc:
            rows = [["--"] * 11 + [f"读取失败：{exc}"]]
        fill_table(
            self.observation_table,
            [
                "赎回篮子标识", "国内发起赎回的交易日", "QMT合同编号", "实际ETF申购退款", "实际ETF现金差额",
                "CFETS美元人民币收盘价", "XOP卖出价格代理", "实际退款反推的每申赎单位XOP股数",
                "PCF扣除现金差额后反推股数", "实际反推股数与PCF校准股数的差异", "是否纳入校准样本", "校准提示",
            ],
            rows or [["--"] * 11 + ["暂无历史反校准"]],
        )

    def write_pcf_point(self) -> None:
        current_detail = self.pcf_tab.current_detail
        trade_day = (
            current_detail.item.trade_date
            if current_detail is not None
            else self.pcf_tab._python_date(self.pcf_tab.date_edit.date())
        )
        try:
            detail = self.pcf_tab.store.ensure_target_detail(trade_day)
            valuation_day = self._metadata_day(detail.metadata.get("PreTradingDay") or "")
            safe_mid = self.fx_store.get_usd_cny_safe_mid(valuation_day)
            if safe_mid is None:
                self.fx_store.ensure_trade_date(valuation_day)
                safe_mid = self.fx_store.get_usd_cny_safe_mid(valuation_day)
            if safe_mid is None:
                raise ValueError(f"缺少 {valuation_day:%Y-%m-%d} SAFE 中间价")
            xop_close = self.price_provider.get_close(valuation_day)
            point = basket_calibration.build_pcf_calibration_point(detail, safe_mid, xop_close)
            self.calibration_store.append_or_replace_pcf_point(point)
        except Exception as exc:
            QMessageBox.critical(self, "PCF 校准失败", str(exc))
            return
        self.refresh_data()
        self.status_label.setText(
            f"已写入 {point.pcf_trading_day:%Y-%m-%d} 159518 PCF股数校准点；"
            f"扣除现金差额后的每申赎单位XOP股数 {point.q_net:.4f}"
        )
        self.calibrationChanged.emit()

    def write_observations(self) -> None:
        if self.result is None:
            QMessageBox.information(self, "暂无篮子", "请先计算 QMT/IB 篮子。")
            return
        written = 0
        skipped: list[str] = []
        for basket in self.result.baskets:
            if basket.refund_amount <= 0 or basket.actual_refund_day is None:
                continue
            try:
                point = self.calibration_store.latest_point_for_day(basket.redeem_day)
                if point is None:
                    raise ValueError("缺少 PCF 校准点")
                settlement_fx = self.fx_store.get_usd_cny_cfets_close(basket.redeem_day)
                if settlement_fx is None:
                    raise ValueError("缺少 CFETS 收盘价")
                xop_price = self.price_provider.get_window_price(basket.redeem_day, self.price_window)
                observation = basket_calibration.build_settlement_observation(
                    basket, point, settlement_fx, xop_price
                )
                if observation is not None:
                    self.observation_store.append_or_replace(observation)
                    written += 1
            except Exception as exc:
                skipped.append(f"篮子{basket.sequence}: {exc}")
        self.refresh_data()
        self.status_label.setText(
            f"已写入/更新 {written} 条历史反校准"
            + (f"；跳过 {' | '.join(skipped)}" if skipped else "")
        )


class RealtimePremiumTab(QWidget):
    def __init__(self, config: dict[str, object], parent=None) -> None:
        super().__init__(parent)
        self.xop_quote: realtime_premium.XopQuote | None = None
        self.domestic_quote: realtime_premium.SinaQuote | None = None
        self.cfets_quote: realtime_premium.CfetsQuote | None = None
        self.pcf_store = szse_pcf.SzsePcfStore(
            Path(str(config.get("szse_pcf_cache_dir") or (ROOT / "szse_pcf_cache")))
        )
        self.pcf_estimate_cash_component: Decimal | None = None
        self.pcf_valuation_day: date | None = None
        self.pcf_detail_error = ""
        self.started = False
        shared_folder_text = str(config.get("shared_folder_path") or "").strip()
        self.shared_folder_path = Path(shared_folder_text).expanduser() if shared_folder_text else None

        self.tws_client = realtime_premium.TwsXopMarketData(
            str(config.get("tws_host") or "127.0.0.1"),
            int(config.get("tws_port") or 7496),
            int(config.get("tws_client_id") or 8888),
            self,
            auto_client_id=bool(config.get("tws_auto_client_id", True)),
        )
        self.sina_client = realtime_premium.SinaPollingClient(parent=self)
        self.cfets_client = realtime_premium.CfetsLatestClient(
            Path(str(config.get("fx_rates_csv_path") or (ROOT / "fx_data" / "fx_rates.csv"))),
            self,
        )
        self._ib_auto_paused_day: date | None = None
        self._sina_auto_paused_day: date | None = None
        self._sina_reconnect_pending = False
        self._auto_connection_timer = QTimer(self)
        self._auto_connection_timer.setInterval(15_000)
        self._auto_connection_timer.timeout.connect(self._automatic_connection_tick)

        self.ib_connect_button = QPushButton("连接IB实时行情")
        self.ib_disconnect_button = QPushButton("断开IB")
        self.ib_disconnect_button.setEnabled(False)
        self.sina_start_button = QPushButton("启动新浪行情")
        self.sina_stop_button = QPushButton("停止新浪行情")
        self.sina_stop_button.setEnabled(False)
        self.cfets_refresh_button = QPushButton("刷新CFETS汇率")

        controls = QHBoxLayout()
        controls.addWidget(self.ib_connect_button)
        controls.addWidget(self.ib_disconnect_button)
        controls.addSpacing(16)
        controls.addWidget(self.sina_start_button)
        controls.addWidget(self.sina_stop_button)
        controls.addSpacing(16)
        controls.addWidget(self.cfets_refresh_button)
        controls.addStretch(1)
        fixed_shares = QLabel("实时总资产估值：996 股 XOP 证券资产 + PCF预估现金差额 / 1,000,000 份")
        fixed_shares.setStyleSheet("font-weight: 600; color: #1d4ed8;")
        controls.addWidget(fixed_shares)

        self.ib_status = QLabel("IB未连接")
        self.sina_status = QLabel("新浪行情未启动")
        self.cfets_status = QLabel("CFETS尚未刷新")
        self.pcf_status = QLabel("PCF尚未加载")
        self.shared_status = QLabel("未配置")
        for label in (self.ib_status, self.sina_status, self.cfets_status, self.pcf_status, self.shared_status):
            label.setObjectName("sourceHint")
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setWordWrap(True)
            label.setMinimumHeight(36)
        statuses = QHBoxLayout()
        statuses.addWidget(QLabel("XOP / TWS"))
        statuses.addWidget(self.ib_status, 1)
        statuses.addSpacing(18)
        statuses.addWidget(QLabel("159518 / 新浪"))
        statuses.addWidget(self.sina_status, 1)
        statuses.addSpacing(18)
        statuses.addWidget(QLabel("USD/CNY / CFETS"))
        statuses.addWidget(self.cfets_status, 1)
        statuses.addSpacing(18)
        statuses.addWidget(QLabel("159518 / PCF"))
        statuses.addWidget(self.pcf_status, 1)
        statuses.addSpacing(18)
        statuses.addWidget(QLabel("共享JSON"))
        statuses.addWidget(self.shared_status, 1)
        status_box = QGroupBox("数据连接状态")
        status_box.setLayout(statuses)

        self.premium_summary_labels: dict[str, QLabel] = {}
        premium_summary = QGroupBox("SZ159518 标普油气 · 实时估值摘要")
        premium_summary_layout = QGridLayout(premium_summary)
        premium_summary_layout.setContentsMargins(8, 8, 8, 8)
        premium_summary_layout.setHorizontalSpacing(8)
        summary_fields = [
            ("现价", "domestic_last"),
            ("Bid总资产预估净值", "nav_bid"),
            ("Ask总资产预估净值", "nav_ask"),
            ("买一同侧溢折价", "bid_premium"),
            ("卖一同侧溢折价", "ask_premium"),
            ("估值状态", "valuation_status"),
        ]
        for column, (title, key) in enumerate(summary_fields):
            field = QWidget()
            field.setObjectName("summaryField")
            field_layout = QVBoxLayout(field)
            field_layout.setContentsMargins(10, 6, 10, 6)
            title_label = QLabel(title)
            title_label.setObjectName("summaryKey")
            value_label = QLabel("--")
            value_label.setObjectName("summaryValue")
            value_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            field_layout.addWidget(title_label)
            field_layout.addWidget(value_label)
            premium_summary_layout.addWidget(field, 0, column)
            self.premium_summary_labels[key] = value_label

        self.quote_table = configured_table()
        self.order_book_table = configured_table()
        self.valuation_table = configured_table()
        quote_box = QGroupBox("实时原始行情")
        quote_layout = QVBoxLayout(quote_box)
        quote_layout.addWidget(self.quote_table)
        order_book_box = QGroupBox("SZ159518 上下五档 · Bid/Ask总资产双估值溢折价")
        order_book_layout = QVBoxLayout(order_book_box)
        order_book_layout.addWidget(self.order_book_table)
        valuation_box = QGroupBox("估值基准与计算公式")
        valuation_layout = QVBoxLayout(valuation_box)
        valuation_layout.addWidget(self.valuation_table)
        detail_splitter = QSplitter(Qt.Vertical)
        detail_splitter.addWidget(quote_box)
        detail_splitter.addWidget(valuation_box)
        detail_splitter.setSizes([250, 390])
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(order_book_box)
        splitter.addWidget(detail_splitter)
        splitter.setSizes([650, 750])

        layout = QVBoxLayout(self)
        layout.addLayout(controls)
        layout.addWidget(status_box)
        layout.addWidget(premium_summary)
        layout.addWidget(splitter, 1)

        self.ib_connect_button.clicked.connect(self.connect_ib_manually)
        self.ib_disconnect_button.clicked.connect(self.disconnect_ib)
        self.sina_start_button.clicked.connect(self.start_sina)
        self.sina_stop_button.clicked.connect(self.stop_sina)
        self.cfets_refresh_button.clicked.connect(self.cfets_client.refresh)
        self.tws_client.quoteUpdated.connect(self.update_xop_quote)
        self.tws_client.statusChanged.connect(self.update_ib_status)
        self.sina_client.quoteUpdated.connect(self.update_domestic_quote)
        self.sina_client.statusChanged.connect(self.update_sina_status)
        self.cfets_client.quoteUpdated.connect(self.update_cfets_quote)
        self.cfets_client.statusChanged.connect(self.update_cfets_status)
        self._set_connection_badge(self.ib_status, "IB未连接", "stopped")
        self._set_connection_badge(self.sina_status, "新浪行情未启动", "stopped")
        self._set_connection_badge(self.cfets_status, "CFETS尚未刷新", "stopped")
        self._set_connection_badge(self.pcf_status, "PCF尚未加载", "stopped")
        self.ensure_shared_document()
        self.refresh_tables()

    def ensure_started(self) -> None:
        if self.started:
            return
        self.started = True
        self.cfets_client.start()
        self._automatic_connection_tick()

    def start_automatic_connections(self) -> None:
        if not self._auto_connection_timer.isActive():
            self._auto_connection_timer.start()
        self._automatic_connection_tick()

    def _automatic_connection_tick(self) -> None:
        current = beijing_now()
        if self._ib_auto_paused_day is not None and self._ib_auto_paused_day != current.date():
            self._ib_auto_paused_day = None
        if self._sina_auto_paused_day is not None and self._sina_auto_paused_day != current.date():
            self._sina_auto_paused_day = None
        if not realtime_premium.is_auto_connection_window(current):
            return
        if self._ib_auto_paused_day != current.date() and not self.tws_client.is_connected():
            self.tws_client.connect_tws()
        if self._sina_auto_paused_day != current.date():
            if self._sina_reconnect_pending and self.sina_client.is_running():
                self._sina_reconnect_pending = False
                self.sina_client.restart()
            elif not self.sina_client.is_running():
                self.start_sina(manual=False)

    @staticmethod
    def _connection_badge_style(state: str) -> str:
        styles = {
            "connected": "background:#dcfce7; color:#166534; border:1px solid #22c55e;",
            "connecting": "background:#fef3c7; color:#92400e; border:1px solid #f59e0b;",
            "error": "background:#fee2e2; color:#991b1b; border:1px solid #ef4444;",
            "stopped": "background:#e5e7eb; color:#475569; border:1px solid #94a3b8;",
        }
        return styles.get(state, styles["stopped"]) + " border-radius:4px; padding:6px 10px; font-weight:700;"

    def _set_connection_badge(self, label: QLabel, text: str, state: str) -> None:
        label.setText(text)
        label.setToolTip(text)
        label.setStyleSheet(self._connection_badge_style(state))

    @staticmethod
    def _status_is_error(text: str) -> bool:
        return any(marker in text for marker in ("失败", "错误", "异常", "中断", "需重新建立"))

    def connect_ib_manually(self) -> None:
        self._ib_auto_paused_day = None
        self.tws_client.connect_tws()

    def configure_tws(
        self,
        host: str,
        port: int,
        client_id: int,
        auto_client_id: bool = True,
    ) -> None:
        self.disconnect_ib(manual=False)
        self.tws_client.host = host
        self.tws_client.port = port
        self.tws_client.client_id = client_id
        self.tws_client.auto_client_id = bool(auto_client_id)
        mode_text = "自动分配" if auto_client_id else f"手工首选 {client_id}"
        self._set_connection_badge(
            self.ib_status,
            f"IB未连接；配置 {host}:{port} / client ID {mode_text}",
            "stopped",
        )

    def configure_shared_folder(self, path: str) -> None:
        value = path.strip()
        self.shared_folder_path = Path(value).expanduser() if value else None
        self.ensure_shared_document()

    def ensure_shared_document(self) -> None:
        if self.shared_folder_path is None:
            self._set_connection_badge(self.shared_status, "未配置", "stopped")
            return
        try:
            document_path = realtime_premium.write_schema_document(self.shared_folder_path)
        except Exception as exc:
            self._set_connection_badge(self.shared_status, f"文档写入失败：{exc}", "error")
        else:
            self._set_connection_badge(self.shared_status, f"文档已写入 {document_path.parent.name}", "connected")

    def start_sina(self, manual: bool = True) -> None:
        if manual:
            self._sina_auto_paused_day = None
            self._sina_reconnect_pending = False
        self.sina_client.start()
        self.sina_start_button.setEnabled(False)
        self.sina_stop_button.setEnabled(True)

    def stop_sina(self, manual: bool = True) -> None:
        if manual:
            self._sina_auto_paused_day = beijing_now().date()
            self._sina_reconnect_pending = False
        self.sina_client.stop()
        self.domestic_quote = None
        self.sina_start_button.setEnabled(True)
        self.sina_stop_button.setEnabled(False)
        self.refresh_tables()

    def disconnect_ib(self, manual: bool = True) -> None:
        if manual:
            self._ib_auto_paused_day = beijing_now().date()
        self.tws_client.disconnect_tws()
        self.xop_quote = None
        self.refresh_tables()

    def shutdown(self) -> None:
        self._auto_connection_timer.stop()
        self.sina_client.stop()
        self.cfets_client.stop()
        self.tws_client.disconnect_tws()

    def update_ib_status(self, text: str, active: bool) -> None:
        if self._status_is_error(text):
            state = "error"
        elif active and ("已连接" in text or "已经连接" in text or "已恢复" in text or "已保留" in text):
            state = "connected"
        elif active:
            state = "connecting"
        else:
            state = "stopped"
        self._set_connection_badge(self.ib_status, text, state)
        self.ib_connect_button.setEnabled(not active)
        self.ib_disconnect_button.setEnabled(active)
        if not active and any(marker in text for marker in ("已断开", "已关闭", "连接失败")):
            self.xop_quote = None
            self.refresh_tables()

    def update_sina_status(self, text: str, active: bool) -> None:
        if self._status_is_error(text):
            state = "error"
            if (
                realtime_premium.is_auto_connection_window(beijing_now())
                and self._sina_auto_paused_day != beijing_now().date()
            ):
                self._sina_reconnect_pending = True
        elif active and "正常" in text:
            state = "connected"
            self._sina_reconnect_pending = False
        elif active:
            state = "connecting"
        else:
            state = "stopped"
        self._set_connection_badge(self.sina_status, text, state)
        running = self.sina_client.is_running()
        self.sina_start_button.setEnabled(not running)
        self.sina_stop_button.setEnabled(running)

    def update_cfets_status(self, text: str, active: bool) -> None:
        state = "error" if self._status_is_error(text) else ("connected" if active else "stopped")
        self._set_connection_badge(self.cfets_status, text, state)

    def _valuation_trade_day(self) -> date:
        if self.domestic_quote is not None:
            source_time = self.domestic_quote.market_time or self.domestic_quote.received_at
            return source_time.date()
        return beijing_now().date()

    def refresh_pcf_estimate_cash_component(self) -> None:
        """Read only the same-day cached PCF; never block live pricing on a network request."""
        trading_day = self._valuation_trade_day()
        self.pcf_valuation_day = trading_day
        self.pcf_estimate_cash_component = None
        self.pcf_detail_error = ""
        if trading_day.weekday() >= 5:
            self._set_connection_badge(self.pcf_status, f"{trading_day:%Y-%m-%d} 非交易日", "stopped")
            return
        if not self.pcf_store.is_fund_detail_cached(trading_day, szse_pcf.TARGET_FUND_CODE):
            self.pcf_detail_error = "等待当日159518 PCF缓存"
            self._set_connection_badge(
                self.pcf_status,
                f"{trading_day:%Y-%m-%d} 等待PCF缓存（申购赎回清单页会后台拉取）",
                "connecting",
            )
            return
        try:
            detail = self.pcf_store.ensure_target_detail(trading_day)
            raw_value = str(detail.metadata.get("EstimateCashComponent") or "").strip()
            if not raw_value:
                raise ValueError("缺少 EstimateCashComponent")
            self.pcf_estimate_cash_component = Decimal(raw_value)
        except Exception as exc:
            self.pcf_detail_error = str(exc)
            self._set_connection_badge(self.pcf_status, f"PCF读取失败：{exc}", "error")
        else:
            self._set_connection_badge(
                self.pcf_status,
                f"{trading_day:%Y-%m-%d} EstimateCashComponent {fmt_money(self.pcf_estimate_cash_component)} 元",
                "connected",
            )

    def update_xop_quote(self, quote: realtime_premium.XopQuote) -> None:
        self.xop_quote = quote
        self.refresh_tables()

    def update_domestic_quote(self, quote: realtime_premium.SinaQuote) -> None:
        self.domestic_quote = quote
        self.refresh_tables()

    def update_cfets_quote(self, quote: realtime_premium.CfetsQuote) -> None:
        self.cfets_quote = quote
        self.refresh_tables()

    @staticmethod
    def _value(value: Decimal | None, places: int) -> str:
        return "--" if value is None else fmt_decimal(value, places)

    @staticmethod
    def _percent(value: Decimal) -> str:
        return f"{value * Decimal('100'):+.4f}%"

    def populate_order_book(self, nav_bid: Decimal | None = None, nav_ask: Decimal | None = None) -> None:
        quote = self.domestic_quote
        rows: list[list[object]] = []
        sides: list[str] = []
        if quote is not None:
            asks = [(index, level) for index, level in enumerate(quote.asks, start=1) if level.price > 0]
            bids = [(index, level) for index, level in enumerate(quote.bids, start=1) if level.price > 0]
            if not asks and quote.ask > 0:
                asks = [(1, realtime_premium.QuoteLevel(quote.ask, quote.ask_volume))]
            if not bids and quote.bid > 0:
                bids = [(1, realtime_premium.QuoteLevel(quote.bid, quote.bid_volume))]
            for index, level in reversed(asks):
                rows.append(self._order_book_row(f"卖{index}", level, nav_bid, nav_ask))
                sides.append("ask")
            for index, level in bids:
                rows.append(self._order_book_row(f"买{index}", level, nav_bid, nav_ask))
                sides.append("bid")
        fill_table(
            self.order_book_table,
            ["档位", "价格", "数量（手）", "相对总篮子Bid", "相对总篮子Ask"],
            rows or [["--", "--", "--", "等待新浪五档行情", "等待估值基准"]],
        )
        for row_index, side in enumerate(sides):
            background = QColor("#fff8f1") if side == "ask" else QColor("#f2fbf5")
            accent = QColor("#c2410c") if side == "ask" else QColor("#047857")
            for column in range(self.order_book_table.columnCount()):
                item = self.order_book_table.item(row_index, column)
                if item is not None:
                    item.setBackground(background)
                    if column in {0, 1}:
                        item.setForeground(accent)
            if side == "bid" and row_index > 0 and sides[row_index - 1] == "ask":
                for column in range(self.order_book_table.columnCount()):
                    item = self.order_book_table.item(row_index, column)
                    if item is not None:
                        font = item.font()
                        font.setBold(True)
                        item.setFont(font)
        self.order_book_table.setColumnWidth(0, 68)
        self.order_book_table.setColumnWidth(1, 82)
        self.order_book_table.setColumnWidth(2, 96)
        self.order_book_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.Stretch)
        self.order_book_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.Stretch)

    def _order_book_row(
        self,
        label: str,
        level: realtime_premium.QuoteLevel,
        nav_bid: Decimal | None,
        nav_ask: Decimal | None,
    ) -> list[object]:
        bid_premium = self._percent(level.price / nav_bid - Decimal("1")) if nav_bid else "--"
        ask_premium = self._percent(level.price / nav_ask - Decimal("1")) if nav_ask else "--"
        lots = Decimal(level.volume) / Decimal("100")
        lots_text = f"{lots:,.0f}" if lots == lots.to_integral() else f"{lots:,.2f}"
        return [label, fmt_decimal(level.price, 3), lots_text, bid_premium, ask_premium]

    def update_premium_summary(self, value: realtime_premium.PremiumValuation | None) -> None:
        last = self.domestic_quote.last if self.domestic_quote else None
        self.premium_summary_labels["domestic_last"].setText(self._value(last, 4))
        if value is None:
            for key in ("nav_bid", "nav_ask", "bid_premium", "ask_premium"):
                self.premium_summary_labels[key].setText("--")
            self.premium_summary_labels["valuation_status"].setText("等待完整行情")
            return
        self.premium_summary_labels["nav_bid"].setText(fmt_decimal(value.nav_bid, 6))
        self.premium_summary_labels["nav_ask"].setText(fmt_decimal(value.nav_ask, 6))
        self.premium_summary_labels["bid_premium"].setText(self._percent(value.domestic_bid_vs_xop_bid))
        self.premium_summary_labels["ask_premium"].setText(self._percent(value.domestic_ask_vs_xop_ask))
        self.premium_summary_labels["valuation_status"].setText("实时")
        for key in ("bid_premium", "ask_premium"):
            label = self.premium_summary_labels[key]
            label.setStyleSheet("color: #c2410c;" if label.text().startswith("+") else "color: #047857;")

    def write_shared_snapshot(self, value: realtime_premium.PremiumValuation) -> None:
        if self.shared_folder_path is None or not self.xop_quote or not self.domestic_quote or not self.cfets_quote:
            return
        try:
            json_path, _document_path = realtime_premium.write_realtime_files(
                self.shared_folder_path,
                self.xop_quote,
                self.domestic_quote,
                self.cfets_quote,
                value,
            )
        except Exception as exc:
            self._set_connection_badge(self.shared_status, f"写入失败：{exc}", "error")
        else:
            self._set_connection_badge(
                self.shared_status,
                f"已更新 {json_path.parent.name}/{json_path.name}",
                "connected",
            )

    def refresh_tables(self) -> None:
        self.refresh_pcf_estimate_cash_component()
        xop_time = self.xop_quote.received_at.strftime("%Y-%m-%d %H:%M:%S") if self.xop_quote else "--"
        domestic_time = "--"
        if self.domestic_quote:
            source_time = self.domestic_quote.market_time or self.domestic_quote.received_at
            domestic_time = source_time.strftime("%Y-%m-%d %H:%M:%S")
        cfets_time = (
            f"{self.cfets_quote.trading_day:%Y-%m-%d} {self.cfets_quote.quote_time}"
            if self.cfets_quote else "--"
        )
        fill_table(
            self.quote_table,
            ["数据项目", "买一 / Bid", "卖一 / Ask", "行情时间", "来源或数量"],
            [
                [
                    "XOP实时价格（美元）",
                    self._value(self.xop_quote.bid if self.xop_quote else None, 4),
                    self._value(self.xop_quote.ask if self.xop_quote else None, 4),
                    xop_time,
                    f"IB TWS；{self.xop_quote.market_data_type}" if self.xop_quote else "IB TWS",
                ],
                [
                    "159518实时价格（人民币）",
                    self._value(self.domestic_quote.bid if self.domestic_quote else None, 3),
                    self._value(self.domestic_quote.ask if self.domestic_quote else None, 3),
                    domestic_time,
                    (
                        f"新浪；买一量 {self.domestic_quote.bid_volume:,} 股 / 卖一量 {self.domestic_quote.ask_volume:,} 股"
                        if self.domestic_quote else "新浪L1"
                    ),
                ],
                [
                    "CFETS美元人民币参考价",
                    self._value(self.cfets_quote.rate if self.cfets_quote else None, 6),
                    self._value(self.cfets_quote.rate if self.cfets_quote else None, 6),
                    cfets_time,
                    "CFETS最新可用小时价",
                ],
                [
                    "159518 PCF预估现金差额（每申赎单位）",
                    self._value(self.pcf_estimate_cash_component, 2),
                    self._value(self.pcf_estimate_cash_component, 2),
                    self.pcf_valuation_day.isoformat() if self.pcf_valuation_day else "--",
                    "深交所PCF；缺失则停止总资产估值",
                ],
            ],
        )

        missing: list[str] = []
        if not self.xop_quote or self.xop_quote.bid is None or self.xop_quote.ask is None:
            missing.append("XOP Bid/Ask（请连接IB）")
        if not self.domestic_quote or self.domestic_quote.bid <= 0 or self.domestic_quote.ask <= 0:
            missing.append("159518买一/卖一（请启动新浪行情）")
        if not self.cfets_quote:
            missing.append("CFETS最新参考价")
        if self.pcf_estimate_cash_component is None:
            missing.append(self.pcf_detail_error or "当日PCF EstimateCashComponent")
        if missing:
            self.update_premium_summary(None)
            self.populate_order_book()
            fill_table(
                self.valuation_table,
                ["状态", "缺少的数据", "说明"],
                [["等待实时数据", "；".join(missing), "三项数据齐全后自动计算"]],
            )
            return
        assert self.xop_quote and self.domestic_quote and self.cfets_quote
        try:
            value = realtime_premium.calculate_premium_valuation(
                self.xop_quote.bid or Decimal("0"),
                self.xop_quote.ask or Decimal("0"),
                self.cfets_quote.rate,
                self.domestic_quote.bid,
                self.domestic_quote.ask,
                estimate_cash_component_cny=self.pcf_estimate_cash_component,
                pcf_trading_day=self.pcf_valuation_day,
            )
        except Exception as exc:
            self.update_premium_summary(None)
            self.populate_order_book()
            fill_table(self.valuation_table, ["状态", "错误"], [["无法计算", str(exc)]])
            return
        self.update_premium_summary(value)
        self.populate_order_book(value.nav_bid, value.nav_ask)
        self.write_shared_snapshot(value)
        fill_table(
            self.valuation_table,
            ["计算项目", "买一 / Bid口径", "卖一 / Ask口径", "完整计算方式"],
            [
                ["XOP证券资产等价", "996 股", "996 股", "PCF成分证券市值反推的实时系数"],
                ["XOP证券资产估值", fmt_money(value.stock_component_bid_cny), fmt_money(value.stock_component_ask_cny), "996 × XOP价格 × CFETS汇率"],
                ["PCF预估现金差额", fmt_money(value.estimate_cash_component_cny), fmt_money(value.estimate_cash_component_cny), "当日159518 PCF EstimateCashComponent"],
                ["一个篮子总资产", fmt_money(value.basket_bid_cny), fmt_money(value.basket_ask_cny), "证券资产估值 + PCF预估现金差额"],
                ["159518每份总资产预估净值", fmt_decimal(value.nav_bid, 6), fmt_decimal(value.nav_ask, 6), "总篮子资产 ÷ 1,000,000份"],
                ["159518国内盘口", fmt_decimal(value.domestic_bid, 3), fmt_decimal(value.domestic_ask, 3), "新浪买一 / 卖一"],
                ["同侧盘口溢折价率", self._percent(value.domestic_bid_vs_xop_bid), self._percent(value.domestic_ask_vs_xop_ask), "买一÷Bid总资产净值-1；卖一÷Ask总资产净值-1"],
                ["可执行保守口径", self._percent(value.executable_sell_domestic_vs_xop_ask), self._percent(value.executable_buy_domestic_vs_xop_bid), "卖国内：买一÷Ask净值-1；买国内：卖一÷Bid净值-1"],
            ],
        )


class XopCloseOrdersTab(QWidget):
    CHECK_COLUMN = 0
    STATUS_COLUMN = 4
    ORDER_ID_COLUMN = 5

    def __init__(self, tws_client: realtime_premium.TwsXopMarketData, parent=None) -> None:
        super().__init__(parent)
        self.tws_client = tws_client
        self.specs_by_ref: dict[str, xop_close_orders.XopCloseOrderSpec] = {}
        self.rows_by_ref: dict[str, int] = {}
        self.confirmed_refs: set[str] = set()

        warning = QLabel(
            "实盘风险提示：本页会发送 BUY MKT DAY 条件单。市价单没有价格保护。"
            "生成预览不会下单；只有勾选订单、解锁实盘按钮，并在每一笔独立确认框中选择“确认发送”后，"
            "该笔订单才会进入 TWS 发送队列。"
        )
        warning.setWordWrap(True)
        warning.setStyleSheet(
            "background:#fff1f2; color:#9f1239; border:1px solid #fecdd3; "
            "border-radius:8px; padding:10px; font-weight:600;"
        )

        self.trade_date_edit = QLineEdit(date.today().strftime("%Y%m%d"))
        self.trade_date_edit.setMaxLength(8)
        self.trade_date_edit.setFixedWidth(120)
        self.trade_date_edit.setPlaceholderText("YYYYMMDD")
        self.basket_count_combo = QComboBox()
        self.basket_count_combo.addItem("1 张", 1)
        self.basket_count_combo.addItem("2 张", 2)
        self.basket_count_combo.setFixedWidth(80)
        self.total_qty_spin = QSpinBox()
        self.total_qty_spin.setRange(801, 1_000_000)
        self.total_qty_spin.setValue(990)
        self.total_qty_spin.setSuffix(" 股")
        self.generate_button = QPushButton()
        self.sync_basket_count_controls()
        self.connect_button = QPushButton("连接IB交易接口")
        self.disconnect_button = QPushButton("断开IB")
        self.disconnect_button.setEnabled(False)
        self.connection_status = QLabel("IB未连接")
        self.connection_status.setObjectName("sourceHint")
        controls = QHBoxLayout()
        controls.addWidget(QLabel("条件单日期（美东）"))
        controls.addWidget(self.trade_date_edit)
        controls.addWidget(QLabel("平仓篮子数"))
        controls.addWidget(self.basket_count_combo)
        controls.addWidget(QLabel("单篮目标平仓数量"))
        controls.addWidget(self.total_qty_spin)
        controls.addWidget(self.generate_button)
        controls.addSpacing(16)
        controls.addWidget(self.connect_button)
        controls.addWidget(self.disconnect_button)
        controls.addWidget(self.connection_status, 1)

        self.order_table = configured_table()
        self.order_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.order_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.unlock_checkbox = QCheckBox("解锁实盘下单（勾选后仍需逐笔确认）")
        self.unlock_checkbox.setStyleSheet("color:#b91c1c; font-weight:600;")
        self.send_button = QPushButton("逐笔确认并发送选中订单（实盘）")
        self.send_button.setEnabled(False)
        self.send_button.setStyleSheet("QPushButton:enabled { background:#b91c1c; color:white; font-weight:700; }")
        send_controls = QHBoxLayout()
        send_controls.addWidget(self.unlock_checkbox)
        send_controls.addWidget(self.send_button)
        send_controls.addStretch(1)
        send_controls.addWidget(QLabel("固定参数：XOP / BUY / MKT / DAY / SMART→ARCA / outsideRth=false"))

        self.event_log = QPlainTextEdit()
        self.event_log.setReadOnly(True)
        self.event_log.setMaximumHeight(150)
        self.event_log.setPlaceholderText("TWS订单回报将在这里显示。")
        log_box = QGroupBox("TWS订单回报：openOrder / orderStatus / execDetails / commissionReport")
        log_layout = QVBoxLayout(log_box)
        log_layout.addWidget(self.event_log)

        layout = QVBoxLayout(self)
        layout.addWidget(warning)
        layout.addLayout(controls)
        layout.addWidget(self.order_table, 1)
        layout.addLayout(send_controls)
        layout.addWidget(log_box)

        self.generate_button.clicked.connect(self.generate_preview)
        self.basket_count_combo.currentIndexChanged.connect(self.handle_basket_count_changed)
        self.connect_button.clicked.connect(self.tws_client.connect_tws)
        self.disconnect_button.clicked.connect(self.tws_client.disconnect_tws)
        self.unlock_checkbox.toggled.connect(self.update_send_button)
        self.send_button.clicked.connect(self.send_selected_orders)
        self.order_table.itemChanged.connect(self.update_send_button)
        self.tws_client.statusChanged.connect(self.update_connection_status)
        self.tws_client.orderEvent.connect(self.handle_order_event)
        self.generate_preview(show_errors=False)

    def selected_basket_count(self) -> int:
        value = self.basket_count_combo.currentData()
        try:
            return int(value)
        except (TypeError, ValueError):
            return 1

    def actual_total_qty(self) -> int:
        return self.total_qty_spin.value() * self.selected_basket_count()

    def sync_basket_count_controls(self) -> None:
        basket_count = self.selected_basket_count()
        minimum_total_qty = xop_close_orders.minimum_total_qty_for_basket_count(basket_count)
        minimum_base_qty = minimum_total_qty // basket_count + 1
        self.total_qty_spin.setMinimum(minimum_base_qty)
        order_count = len(xop_close_orders.trigger_times_for_basket_count(basket_count))
        self.generate_button.setText(f"生成{order_count}张订单预览（不发送）")

    def handle_basket_count_changed(self, _index: int) -> None:
        self.sync_basket_count_controls()
        self.generate_preview(show_errors=False)

    def generate_preview(self, _checked: bool = False, *, show_errors: bool = True) -> None:
        try:
            trade_day = xop_close_orders.parse_trade_date(self.trade_date_edit.text())
            basket_count = self.selected_basket_count()
            base_qty = self.total_qty_spin.value()
            total_qty = self.actual_total_qty()
            specs = xop_close_orders.generate_order_specs(
                trade_day,
                total_qty,
                basket_count=basket_count,
            )
            for spec in specs:
                xop_close_orders.validate_future_trigger(spec)
        except Exception as exc:
            self.specs_by_ref = {}
            self.rows_by_ref = {}
            fill_table(self.order_table, ["状态"], [[str(exc)]])
            if show_errors:
                QMessageBox.warning(self, "无法生成条件单", str(exc))
            self.update_send_button()
            return

        self.specs_by_ref = {item.order_ref: item for item in specs}
        self.rows_by_ref = {item.order_ref: row for row, item in enumerate(specs)}
        self.confirmed_refs.clear()
        headers = [
            "选择", "序号", "数量", "触发时间（美东）", "状态", "TWS OrderId",
            "方向", "类型", "TIF", "盘前盘后", "延长时段检查条件", "条件满足时", "合约", "OrderRef",
        ]
        rows = [
            [
                "", item.sequence, item.quantity, item.condition_time, "仅预览，未发送", "--",
                item.action, item.order_type, item.tif, "禁止", "否", "激活订单",
                "XOP / STK / SMART / ARCA / USD", item.order_ref,
            ]
            for item in specs
        ]
        self.order_table.blockSignals(True)
        fill_table(self.order_table, headers, rows)
        for row, spec in enumerate(specs):
            checkbox = QTableWidgetItem("")
            checkbox.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            checkbox.setCheckState(Qt.Unchecked)
            checkbox.setData(Qt.UserRole, spec.order_ref)
            checkbox.setToolTip("默认不选择；只有手动勾选的订单才会进入逐笔确认流程")
            self.order_table.setItem(row, self.CHECK_COLUMN, checkbox)
        self.order_table.blockSignals(False)
        self.order_table.setColumnWidth(0, 55)
        self.order_table.setColumnWidth(1, 55)
        self.order_table.setColumnWidth(2, 75)
        self.order_table.setColumnWidth(3, 245)
        self.order_table.setColumnWidth(4, 180)
        self.order_table.setColumnWidth(5, 105)
        self.order_table.setColumnWidth(12, 230)
        self.order_table.setColumnWidth(13, 320)
        self.unlock_checkbox.setChecked(False)
        self.event_log.appendPlainText(
            f"已生成 {trade_day:%Y-%m-%d} 的{len(specs)}张预览，"
            f"平仓 {basket_count} 张，单篮目标 {base_qty:,} 股，"
            f"合计 {sum(item.quantity for item in specs):,} 股；尚未发送。"
        )
        self.update_send_button()

    def selected_specs(self) -> list[xop_close_orders.XopCloseOrderSpec]:
        selected: list[xop_close_orders.XopCloseOrderSpec] = []
        for row in range(self.order_table.rowCount()):
            item = self.order_table.item(row, self.CHECK_COLUMN)
            if item is None or item.checkState() != Qt.Checked:
                continue
            order_ref = str(item.data(Qt.UserRole) or "")
            spec = self.specs_by_ref.get(order_ref)
            if spec is not None and order_ref not in self.confirmed_refs:
                selected.append(spec)
        return selected

    def update_send_button(self, *_args) -> None:
        connected = self.tws_client.is_connected()
        self.connect_button.setEnabled(not connected)
        self.disconnect_button.setEnabled(connected)
        self.send_button.setEnabled(bool(connected and self.unlock_checkbox.isChecked() and self.selected_specs()))

    def update_connection_status(self, text: str, _active: bool) -> None:
        self.connection_status.setText(text)
        self.update_send_button()

    def send_selected_orders(self) -> None:
        specs = self.selected_specs()
        if not self.unlock_checkbox.isChecked() or not specs:
            return
        if not self.tws_client.is_connected():
            QMessageBox.warning(self, "IB未连接", "请先连接IB交易接口。")
            return
        for index, spec in enumerate(specs, start=1):
            try:
                xop_close_orders.validate_future_trigger(spec)
            except Exception as exc:
                self._set_row_status(spec.order_ref, f"禁止发送：{exc}")
                continue
            text = (
                f"这是实盘市价条件单（第 {index}/{len(specs)} 笔）\n\n"
                f"合约：XOP / STK / SMART / primaryExchange=ARCA\n"
                f"方向：BUY\n订单类型：MKT（无价格保护）\n"
                f"数量：{spec.quantity} 股\nTIF：DAY\n"
                f"触发条件：晚于 {spec.condition_time}\n"
                f"盘前盘后：禁止\n延长时段检查条件：否\n"
                f"OrderRef：{spec.order_ref}\n\n"
                "确认将这一笔订单发送到 TWS 吗？"
            )
            answer = QMessageBox.question(
                self,
                "逐笔实盘确认",
                text,
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                self._set_row_status(spec.order_ref, "用户取消，未发送")
                continue
            if self.tws_client.submit_confirmed_order(spec):
                self.confirmed_refs.add(spec.order_ref)
                self._set_row_status(spec.order_ref, "已确认，等待TWS回报")
                row = self.rows_by_ref.get(spec.order_ref)
                if row is not None:
                    check_item = self.order_table.item(row, self.CHECK_COLUMN)
                    if check_item is not None:
                        self.order_table.blockSignals(True)
                        check_item.setCheckState(Qt.Unchecked)
                        check_item.setFlags(Qt.ItemIsEnabled)
                        self.order_table.blockSignals(False)
        self.unlock_checkbox.setChecked(False)
        self.update_send_button()

    def _set_row_status(self, order_ref: str, text: str, order_id: object = None) -> None:
        row = self.rows_by_ref.get(order_ref)
        if row is None:
            return
        if order_id not in (None, "", 0):
            self.order_table.setItem(row, self.ORDER_ID_COLUMN, QTableWidgetItem(str(order_id)))
        self.order_table.setItem(row, self.STATUS_COLUMN, QTableWidgetItem(text))

    def handle_order_event(self, payload: object) -> None:
        if not isinstance(payload, dict):
            return
        event = str(payload.get("event") or "")
        order_ref = str(payload.get("order_ref") or "")
        status = str(payload.get("status") or "")
        message = str(payload.get("message") or "")
        order_id = payload.get("order_id")
        detail = " / ".join(item for item in (event, status, message) if item)
        if order_ref:
            self._set_row_status(order_ref, detail, order_id)
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.event_log.appendPlainText(
            f"[{timestamp}] {order_ref or '--'} | orderId={order_id or '--'} | {detail or payload}"
        )


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("ETF 赎回收益计算器")
        self.resize(1420, 900)
        self.config = load_config()
        self.overrides = engine.load_overrides(OVERRIDES_PATH)
        self.predicted_refund_store = settlement_estimator.PredictedRefundStore(
            Path(str(self.config.get("predicted_refund_csv_path") or DEFAULT_CONFIG["predicted_refund_csv_path"]))
        )
        self.predicted_refunds: dict[str, settlement_estimator.PredictedRefund] = {}
        self.result: engine.CalculationResult | None = None
        self.basket_by_id: dict[str, engine.BasketResult] = {}
        self.venue_by_id: dict[str, engine.VenueClose] = {}
        self.refreshing = False
        self._shutting_down = False
        self._prediction_worker_thread: QThread | None = None
        self._prediction_worker: PredictedRefundWorker | None = None

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
        self.prediction_button = QPushButton("预测重算")
        self.prediction_status_label = QLabel("预估篮子资产：读取本地缓存")
        self.prediction_status_label.setObjectName("sourceHint")
        self.prediction_status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
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
        prediction_controls = QHBoxLayout()
        prediction_controls.addWidget(self.prediction_button)
        prediction_controls.addWidget(self.prediction_status_label, 1)
        overview_layout.addLayout(prediction_controls)
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
        self.ib_self_close_tab = IbSelfCloseTab()
        self.pcf_tab = SzsePcfTab(
            Path(str(self.config.get("szse_pcf_cache_dir") or (ROOT / "szse_pcf_cache"))),
            Path(str(self.config.get("fx_rates_csv_path") or (ROOT / "fx_data" / "fx_rates.csv"))),
        )
        self.arrival_calibration_tab = ArrivalCalibrationTab(self.config, self.pcf_tab)
        self.realtime_premium_tab = RealtimePremiumTab(self.config)
        self.xop_close_orders_tab = XopCloseOrdersTab(self.realtime_premium_tab.tws_client)
        tabs = QTabWidget()
        tabs.addTab(overview, "篮子汇总")
        tabs.addTab(self.mapping_tab, "篮子配对图")
        tabs.addTab(self.pcf_tab, "申购赎回清单")
        tabs.addTab(self.arrival_calibration_tab, "到账预估与校准工具")
        tabs.addTab(self.realtime_premium_tab, "实时溢价率")
        tabs.addTab(self.xop_close_orders_tab, "XOP晚间平仓条件单")
        tabs.addTab(domestic_splitter, "国内 FIFO")
        tabs.addTab(ib_widget, "IB 对冲")
        tabs.addTab(self.ib_self_close_tab, "国内外碎单自平")
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
        self.prediction_button.clicked.connect(self.recalculate_predicted_refunds)
        self.fx_spin.editingFinished.connect(self.change_fx)
        self.mapping_button.clicked.connect(self.edit_ib_mapping)
        self.clear_mapping_button.clicked.connect(self.clear_ib_mapping)
        self.ib_self_close_tab.mappingRequested.connect(self.edit_strategy_self_mapping)
        self.ib_self_close_tab.mappingCleared.connect(self.clear_strategy_self_mapping)
        self.tabs.currentChanged.connect(self.handle_tab_changed)
        self.arrival_calibration_tab.calibrationChanged.connect(self.calculate)
        self.setup_menu()
        self.load_predicted_refunds()
        self.update_source_label()
        self.restart_watcher()
        QTimer.singleShot(0, self.calculate)
        QTimer.singleShot(0, self.pcf_tab.startup_prefetch_if_needed)
        QTimer.singleShot(0, self.realtime_premium_tab.start_automatic_connections)

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
        self.predicted_refund_store = settlement_estimator.PredictedRefundStore(
            Path(str(self.config.get("predicted_refund_csv_path") or DEFAULT_CONFIG["predicted_refund_csv_path"]))
        )
        self.load_predicted_refunds()
        self.realtime_premium_tab.configure_tws(
            str(self.config.get("tws_host") or "127.0.0.1"),
            int(self.config.get("tws_port") or 7496),
            int(self.config.get("tws_client_id") or 8888),
            bool(self.config.get("tws_auto_client_id", True)),
        )
        self.realtime_premium_tab.configure_shared_folder(
            str(self.config.get("shared_folder_path") or "")
        )
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
        qmt3 = str(self.config.get("qmt3_path") or "").strip()
        return (
            {
                "QMT1": str(self.config.get("qmt1_path") or "").strip(),
                "QMT2": qmt2 or None,
                "QMT3": qmt3 or None,
            },
            str(self.config.get("ib_path") or "").strip(),
        )

    def update_source_label(self) -> None:
        qmt, ib = self.input_paths()
        qmt2 = Path(qmt["QMT2"]).name if qmt["QMT2"] else "未配置"
        qmt3 = Path(qmt["QMT3"]).name if qmt["QMT3"] else "未配置"
        self.source_label.setText(
            f"QMT1 {Path(qmt['QMT1']).name if qmt['QMT1'] else '--'} | QMT2 {qmt2} | QMT3 {qmt3} | IB {Path(ib).name if ib else '--'}"
        )

    def restart_watcher(self) -> None:
        current = self.watcher.files()
        if current:
            self.watcher.removePaths(current)
        if not bool(self.config.get("auto_refresh", True)):
            return
        qmt, ib = self.input_paths()
        paths = [
            str(Path(item).expanduser().resolve())
            for item in [qmt.get("QMT1"), qmt.get("QMT2"), qmt.get("QMT3"), ib]
            if item
        ]
        existing = [item for item in paths if Path(item).exists()]
        if existing:
            self.watcher.addPaths(existing)

    def load_predicted_refunds(self) -> None:
        try:
            self.predicted_refunds = self.predicted_refund_store.by_basket_id()
            usable = sum(
                1
                for item in self.predicted_refunds.values()
                if (
                    item.model_version == settlement_estimator.PREDICTED_BASKET_MODEL_VERSION
                    and item.predicted_basket_asset_cny is not None
                )
            )
            suffix = "；旧退款缓存请点击“预测重算”更新" if usable < len(self.predicted_refunds) else ""
            self.prediction_status_label.setText(
                f"预估篮子资产：已读取 {len(self.predicted_refunds)} 条，其中当前总资产模型 {usable} 条{suffix}"
            )
        except Exception as exc:
            self.predicted_refunds = {}
            self.prediction_status_label.setText(f"预估篮子资产缓存读取失败：{exc}")

    def predicted_basket_asset(self, basket: engine.BasketResult) -> settlement_estimator.PredictedRefund | None:
        prediction = self.predicted_refunds.get(basket.id)
        if prediction is None:
            return None
        if (
            prediction.redeem_day != basket.redeem_day
            or prediction.contract_no != basket.contract_no
            or prediction.redeem_qty != basket.redeem_qty
            or prediction.model_version != settlement_estimator.PREDICTED_BASKET_MODEL_VERSION
            or prediction.predicted_basket_asset_cny is None
        ):
            return None
        return prediction

    def predicted_basket_asset_text(self, basket: engine.BasketResult) -> str:
        prediction = self.predicted_basket_asset(basket)
        return fmt_money(prediction.predicted_basket_asset_cny) if prediction is not None else "--"

    def predicted_total_pnl_cny(self, basket: engine.BasketResult) -> Decimal | None:
        """Estimate total basket P&L using the predicted domestic basket asset."""
        prediction = self.predicted_basket_asset(basket)
        if prediction is None or prediction.predicted_basket_asset_cny is None:
            return None
        return prediction.predicted_basket_asset_cny - basket.domestic_cost + basket.ib_pnl_cny

    def predicted_total_pnl_text(self, basket: engine.BasketResult) -> str:
        value = self.predicted_total_pnl_cny(basket)
        return fmt_money(value) if value is not None else "--"

    @staticmethod
    def has_actual_basket_asset(basket: engine.BasketResult) -> bool:
        has_cash_difference = any(item.action == "ETF 现金差额" for item in basket.cash_flows)
        has_refund = basket.actual_refund_day is not None or basket.manual_refund_applied
        return has_cash_difference and has_refund

    def total_pnl_prediction_deviation_text(self, basket: engine.BasketResult) -> str:
        predicted_total = self.predicted_total_pnl_cny(basket)
        if predicted_total is None or not self.has_actual_basket_asset(basket):
            return "--"
        deviation = basket.total_pnl_cny - predicted_total
        return f"{engine.money(deviation):+,.2f}"

    @staticmethod
    def actual_basket_asset_text(basket: engine.BasketResult) -> str:
        return fmt_money(basket.refund_amount + basket.cash_difference)

    def basket_asset_prediction_error_text(self, basket: engine.BasketResult) -> str:
        prediction = self.predicted_basket_asset(basket)
        if prediction is None or not self.has_actual_basket_asset(basket):
            return "--"
        assert prediction.predicted_basket_asset_cny is not None
        if prediction.predicted_basket_asset_cny == 0:
            return "--"
        deviation = (
            (basket.refund_amount + basket.cash_difference) / prediction.predicted_basket_asset_cny
            - Decimal("1")
        )
        return f"{deviation * Decimal('100'):+.4f}%"

    def recalculate_predicted_refunds(self) -> None:
        if getattr(self, "_shutting_down", False) or self._prediction_worker_thread is not None:
            return
        if self.result is None or not self.result.baskets:
            QMessageBox.information(self, "暂无篮子", "请先完成篮子计算后再重算预估篮子资产。")
            return
        baskets = tuple(self.result.baskets)
        base_client_id = int(self.config.get("tws_client_id") or 8888)
        worker_client_id = min(2_147_483_647, base_client_id + 1000)
        self.prediction_button.setEnabled(False)
        self.prediction_status_label.setText(
            "正在读取XOP美东15:59分钟收盘价、"
            f"T日CFETS {settlement_estimator.PREDICTED_BASKET_FX_QUOTE_TIME}价与PCF预估现金差额..."
        )
        thread = QThread(self)
        worker = PredictedRefundWorker(
            baskets,
            Path(str(self.config.get("xop_price_csv_path") or DEFAULT_CONFIG["xop_price_csv_path"])),
            Path(str(self.config.get("fx_rates_csv_path") or DEFAULT_CONFIG["fx_rates_csv_path"])),
            Path(str(self.config.get("szse_pcf_cache_dir") or DEFAULT_CONFIG["szse_pcf_cache_dir"])),
            Path(str(self.config.get("predicted_refund_csv_path") or DEFAULT_CONFIG["predicted_refund_csv_path"])),
            str(self.config.get("tws_host") or "127.0.0.1"),
            int(self.config.get("tws_port") or 7496),
            worker_client_id,
        )
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self.handle_predicted_refunds_finished)
        worker.finished.connect(thread.quit)
        worker.finished.connect(worker.deleteLater)
        thread.finished.connect(thread.deleteLater)
        self._prediction_worker_thread = thread
        self._prediction_worker = worker
        thread.start()

    def handle_predicted_refunds_finished(self, result: object) -> None:
        payload = dict(result)
        self._prediction_worker_thread = None
        self._prediction_worker = None
        if self._shutting_down:
            return
        self.prediction_button.setEnabled(True)
        fatal_error = str(payload.get("fatal_error") or "")
        if fatal_error:
            self.prediction_status_label.setText(f"预估篮子资产重算失败：{fatal_error}")
            QMessageBox.critical(self, "预估篮子资产重算失败", fatal_error)
            return
        predictions = list(payload.get("predictions") or [])
        errors = [str(item) for item in (payload.get("errors") or [])]
        if predictions:
            self.predicted_refunds.update({item.basket_id: item for item in predictions})
        selected_id = self.selected_basket_id()
        if self.result is not None:
            self.populate_baskets(selected_id)
        self.prediction_status_label.setText(
            f"预估篮子资产：已重算 {len(predictions)} 个篮子"
            + (f"；失败 {len(errors)} 个：{' | '.join(errors[:3])}" if errors else "")
        )

    def schedule_refresh(self, _path: str) -> None:
        self.refresh_timer.start()

    def calculate(self) -> None:
        if getattr(self, "_shutting_down", False) or self.refreshing:
            return
        self.refreshing = True
        selected_id = self.selected_basket_id()
        qmt_paths, ib_path = self.input_paths()
        configuration_errors = input_path_errors(qmt_paths, ib_path)
        if configuration_errors:
            self.result = None
            self.basket_by_id = {}
            self.venue_by_id = {}
            self.status_label.setText(
                "数据源尚未就绪："
                + "；".join(configuration_errors)
                + "。请点击右上角“数据源”完成配置。"
            )
            self.refreshing = False
            self.restart_watcher()
            return
        self.status_label.setText("正在读取完整交割单并计算...")
        QApplication.processEvents()
        try:
            result = engine.calculate(
                qmt_paths,
                ib_path,
                Decimal(str(self.fx_spin.value())),
                self.overrides,
                self.market_holidays(),
                int(self.config.get("transfer_contract_gap") or engine.DEFAULT_TRANSFER_CONTRACT_GAP),
                qmt_time_root=str(self.config.get("shared_folder_path") or ""),
            )
        except Exception as exc:
            self.status_label.setText(f"计算失败：{exc}")
            QMessageBox.critical(self, "计算失败", f"{exc}\n\n{traceback.format_exc()}")
            self.refreshing = False
            self.restart_watcher()
            return
        self.result = result
        self.basket_by_id = {item.id: item for item in result.baskets}
        self.venue_by_id = {item.id: item for item in getattr(result, "venue_closes", ())}
        if hasattr(self, "load_predicted_refunds"):
            self.load_predicted_refunds()
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
        elif self.tabs.widget(index) is self.arrival_calibration_tab:
            self.arrival_calibration_tab.refresh_data()
        elif self.tabs.widget(index) is self.realtime_premium_tab:
            self.realtime_premium_tab.ensure_started()

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        self.refresh_timer.stop()
        self.watcher.blockSignals(True)
        worker = self._prediction_worker
        if worker is not None:
            worker.cancel()
        thread = self._prediction_worker_thread
        if thread is not None and thread.isRunning():
            thread.requestInterruption()
            thread.quit()
        self.pcf_tab.shutdown()
        self.realtime_premium_tab.shutdown()

    def closeEvent(self, event) -> None:
        self.shutdown()
        super().closeEvent(event)

    def populate_all(self, selected_id: str | None) -> None:
        assert self.result is not None
        self.populate_summary()
        self.populate_baskets(selected_id)
        self.populate_venue_closes()
        self.populate_account_transfers()
        self.populate_warnings()
        self.mapping_tab.update_data(self.result)
        self.ib_self_close_tab.update_data(self.result)
        self.pcf_tab.suggest_date(self.result.qmt_latest_day or date.today())
        self.arrival_calibration_tab.update_data(self.result)
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
            ("碎单自平收益 RMB", fmt_money(self.result.strategy_self_total_cny)),
            ("未闭合 IB", f"SELL {self.result.unallocated_ib_sell_qty:,} / BUY {self.result.unallocated_ib_buy_qty:,}"),
            ("手工休市日", f"{len(self.market_holidays())}"),
            ("跨账户调仓", f"{len(self.result.account_transfers)}"),
        ]
        for column, (name, value) in enumerate(values):
            field = QWidget()
            pnl_value = None
            if name == "已结算收益 RMB":
                pnl_value = self.result.settled_total_cny
            elif name == "碎单自平收益 RMB":
                pnl_value = self.result.strategy_self_total_cny
            if pnl_value is None:
                field_name = "summaryField"
                value_name = "summaryValue"
            elif pnl_value >= 0:
                field_name = "summaryFieldPositive"
                value_name = "summaryValuePositive"
            else:
                field_name = "summaryFieldNegative"
                value_name = "summaryValueNegative"
            field.setObjectName(field_name)
            layout = QVBoxLayout(field)
            layout.setContentsMargins(8, 5, 8, 5)
            key = QLabel(name)
            key.setObjectName("summaryKey")
            val = QLabel(value)
            val.setObjectName(value_name)
            layout.addWidget(key)
            layout.addWidget(val)
            self.summary_grid.addWidget(field, 0, column)

    def populate_baskets(self, selected_id: str | None) -> None:
        assert self.result is not None
        detailed = self.detail_button.isChecked()
        if detailed:
            headers = [
                "轮次", "状态", "赎回日", "预计T+3", "实际T+3", "预计T+6到账", "实际到账",
                "来源", "合同编号", "国内成本", "现金差额", "退款", "篮子资产", "预估篮子资产", "篮子偏差", "预估总收益", "收益偏差", "国内盈亏",
                "IB股数", "IB净盈亏USD", "IB净盈亏RMB", "合计RMB", "IB映射",
            ]
        else:
            headers = [
                "轮次", "状态", "赎回日", "T+3", "T+6到账", "国内成本",
                "现金差额", "退款", "篮子资产", "预估篮子资产", "篮子偏差", "预估总收益", "收益偏差", "国内盈亏",
                "合计RMB", "IB映射",
            ]
        rows = []
        payloads = []
        for basket in self.result.baskets:
            estimate_values = [
                fmt_money(basket.domestic_cost),
                fmt_money(basket.cash_difference),
                refund_amount_text(basket),
                self.actual_basket_asset_text(basket),
                self.predicted_basket_asset_text(basket),
                self.basket_asset_prediction_error_text(basket),
                self.predicted_total_pnl_text(basket),
                self.total_pnl_prediction_deviation_text(basket),
                fmt_money(basket.domestic_pnl),
            ]
            ib_values = [
                f"{basket.hedge_target - basket.ib_open_shortfall:,}/{basket.hedge_target:,}",
                fmt_decimal(basket.ib_pnl_usd),
                fmt_money(basket.ib_pnl_cny),
            ]
            result_values = [fmt_money(basket.total_pnl_cny), ib_mapping_text(basket)]
            if detailed:
                rows.append(
                    [
                        basket.sequence,
                        basket.status,
                        basket_summary_date_text(basket.redeem_day, detailed=True),
                        basket_summary_date_text(basket.expected_cash_difference_day, detailed=True),
                        basket_summary_date_text(basket.actual_cash_difference_day, detailed=True),
                        basket_summary_date_text(basket.expected_refund_day, detailed=True),
                        basket_summary_date_text(basket.actual_refund_day, detailed=True),
                        basket.source,
                        basket.contract_no,
                        *estimate_values,
                        *ib_values,
                        *result_values,
                    ]
                )
            else:
                rows.append(
                    [
                        basket.sequence,
                        basket.status,
                        basket_summary_date_text(basket.redeem_day, detailed=False),
                        basket_summary_date_text(basket.expected_cash_difference_day, detailed=False),
                        basket_summary_date_text(basket.expected_refund_day, detailed=False),
                        *estimate_values,
                        *result_values,
                    ]
                )
            payloads.append(basket.id)
        fill_table(self.basket_table, headers, rows, payloads=payloads)
        basket_deviation_header = self.basket_table.horizontalHeaderItem(headers.index("篮子偏差"))
        if basket_deviation_header is not None:
            basket_deviation_header.setToolTip("篮子资产 ÷ 预估篮子资产 − 1")
        predicted_pnl_header = self.basket_table.horizontalHeaderItem(headers.index("预估总收益"))
        if predicted_pnl_header is not None:
            predicted_pnl_header.setToolTip("预估篮子资产 − 国内成本 + IB盈亏RMB")
        pnl_deviation_header = self.basket_table.horizontalHeaderItem(headers.index("收益偏差"))
        if pnl_deviation_header is not None:
            pnl_deviation_header.setToolTip("实际合计RMB − 预估总收益")
        status_colors = {
            "已结算": ("#ecfdf3", "#14532d"),
            "待现金差额": ("#fff7ed", "#9a3412"),
            "待现金替代款": ("#fffbeb", "#92400e"),
            "IB未完全匹配": ("#fef2f2", "#991b1b"),
            "国内库存不足": ("#fef2f2", "#991b1b"),
        }
        selected_row = 0
        refund_column = headers.index("退款")
        for row, basket in enumerate(self.result.baskets):
            background, foreground = status_colors.get(basket.status, ("#ffffff", "#111827"))
            for column in range(self.basket_table.columnCount()):
                self.basket_table.item(row, column).setBackground(QColor(background))
                self.basket_table.item(row, column).setForeground(QColor(foreground))
            if basket.manual_refund_applied:
                refund_item = self.basket_table.item(row, refund_column)
                if refund_item is not None:
                    refund_item.setBackground(QColor("#f3e8ff"))
                    refund_item.setForeground(QColor("#6b21a8"))
            if basket.id == selected_id:
                selected_row = row
        if self.basket_table.rowCount():
            self.basket_table.selectRow(selected_row)
            self.basket_table.horizontalScrollBar().setValue(0)

    def populate_venue_closes(self) -> None:
        assert self.result is not None
        headers = [
            "状态", "日期", "时间", "账户", "合同编号", "数量", "卖出净额", "FIFO成本", "场内收益",
            "IB开/目标/平", "IB盈亏USD", "IB盈亏RMB", "合计RMB", "配对", "库存/IB缺口",
        ]
        rows = [
            [
                item.status,
                item.trade_day.isoformat(),
                item.trade_dt.strftime("%H:%M:%S") if item.trade_dt else "--",
                item.source,
                item.contract_no,
                f"{item.qty:,}",
                fmt_money(item.proceeds),
                fmt_money(item.cost),
                fmt_money(item.pnl),
                f"{item.ib_open_qty:,}/{item.hedge_target:,}/{item.ib_close_qty:,}",
                fmt_decimal(item.ib_trade_pnl_usd),
                fmt_money(item.ib_pnl_cny),
                fmt_money(item.total_pnl_cny),
                "人工" if item.manual_ib_mapping else "自动",
                f"{item.inventory_shortfall:,}/{item.ib_open_shortfall + item.ib_close_shortfall:,}",
            ]
            for item in self.result.venue_closes
        ]
        fill_table(
            self.venue_closes_table,
            headers,
            rows or [["--"] * (len(headers) - 1) + ["暂无场内卖出"]],
        )

    def populate_account_transfers(self) -> None:
        assert self.result is not None
        headers = [
            "日期", "匹配规则", "数量", "卖出账户", "卖出合同", "卖出净额", "原FIFO成本", "调仓已实现盈亏",
            "买入账户", "买入合同", "新买入成本", "纳入赎回成本", "QMT3承接IB", "合同号间隔", "库存缺口",
        ]
        rows = [
            [
                item.trade_day.isoformat(),
                item.kind,
                f"{item.qty:,}",
                item.sell_source,
                item.sell_contract_no,
                fmt_money(item.sell_proceeds),
                fmt_money(item.sell_fifo_cost),
                fmt_money(item.realized_pnl),
                item.buy_source,
                item.buy_contract_no,
                fmt_money(item.buy_cost),
                fmt_money(item.carried_cost) if item.carried_cost is not None else "--",
                f"{sum(hedge.qty for hedge in item.qmt3_hedge_open):,}/{item.qmt3_hedge_target:,}",
                item.contract_gap if item.contract_gap is not None else "--",
                f"{item.inventory_shortfall:,}",
            ]
            for item in self.result.account_transfers
        ]
        fill_table(
            self.transfer_table,
            headers,
            rows or [["--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "--", "暂无自动识别的跨账户调仓"]],
        )

    def populate_warnings(self) -> None:
        assert self.result is not None
        rows: list[list[object]] = []
        for basket in self.result.baskets:
            for warning in basket.warnings:
                rows.append([basket.sequence, basket.redeem_day.isoformat(), basket.contract_no, basket.status, warning])
        for close in self.result.venue_closes:
            if not close.is_complete:
                rows.append([
                    "自平",
                    close.trade_day.isoformat(),
                    close.contract_no,
                    close.status,
                    f"国内缺口 {close.inventory_shortfall:,}；IB开仓缺口 {close.ib_open_shortfall:,}；IB平仓缺口 {close.ib_close_shortfall:,}",
                ])
            for warning in close.warnings:
                rows.append(["自平", close.trade_day.isoformat(), close.contract_no, "配对提示", warning])
        rows.append(["全局", "--", "--", "未闭合IB", f"SELL {self.result.unallocated_ib_sell_qty:,} 股；BUY {self.result.unallocated_ib_buy_qty:,} 股"])
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
        BasketDetailDialog(
            basket,
            self.is_manual_virtual_close(basket.id),
            self.manual_refund_amount(basket.id),
            self.set_basket_manual_overrides,
            self,
        ).exec_()

    def is_manual_virtual_close(self, basket_id: str) -> bool:
        override = self.overrides.get(basket_id, {})
        return bool(override.get("manual_virtual_close"))

    def manual_refund_amount(self, basket_id: str) -> Decimal | None:
        return engine.manual_refund_override_amount(self.overrides.get(basket_id, {}))

    def set_manual_virtual_close(self, basket_id: str, enabled: bool) -> None:
        MainWindow.set_basket_manual_overrides(
            self,
            basket_id,
            enabled,
            MainWindow.manual_refund_amount(self, basket_id),
        )

    def set_basket_manual_overrides(
        self,
        basket_id: str,
        virtual_close_enabled: bool,
        manual_refund_amount: Decimal | None,
    ) -> None:
        override = dict(self.overrides.get(basket_id, {}))
        if virtual_close_enabled:
            override["manual_virtual_close"] = True
            override.pop("close_trade_ids", None)
        else:
            override.pop("manual_virtual_close", None)
        if manual_refund_amount is not None and manual_refund_amount > 0:
            override["manual_refund_amount"] = str(engine.money(manual_refund_amount))
        else:
            override.pop("manual_refund_amount", None)
        if override:
            self.overrides[basket_id] = override
        else:
            self.overrides.pop(basket_id, None)
        engine.save_overrides(OVERRIDES_PATH, self.overrides)
        self.calculate()

    def refresh_details(self) -> None:
        basket = self.selected_basket()
        if basket is None:
            return
        domestic_rows = [
            [
                item.trade_day.isoformat(),
                item.source,
                item.contract_no,
                f"{item.qty:,}",
                fmt_money(item.cost),
                f"{sum(hedge.qty for hedge in item.qmt3_hedge_open):,}/{item.qmt3_hedge_target:,}",
            ]
            for item in basket.domestic_matches
        ]
        fill_table(
            self.domestic_matches_table,
            ["买入日期", "来源账户", "买入合同", "使用数量", "分摊成本", "QMT3承接IB"],
            domestic_rows,
        )
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
            [
                item.dt.strftime("%Y-%m-%d %H:%M:%S"),
                item.side,
                item.role or "--",
                item.qty,
                fmt_decimal(item.price, 4),
                fmt_money(item.gross),
                fmt_decimal(item.commission),
                item.trade_id,
            ]
            for item in slices
        ]
        fill_table(table, ["时间（IB账单）", "方向", "角色", "数量", "价格", "成交额USD", "佣金USD", "交易ID"], rows)

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
        override = dict(self.overrides.get(basket.id, {}))
        override.update({
            "open_trade_ids": dialog.selected_ids(dialog.open_table),
            "close_trade_ids": dialog.selected_ids(dialog.close_table),
        })
        override.pop("manual_virtual_close", None)
        self.overrides[basket.id] = override
        engine.save_overrides(OVERRIDES_PATH, self.overrides)
        self.calculate()

    def clear_ib_mapping(self) -> None:
        basket = self.selected_basket()
        if basket is None or basket.id not in self.overrides:
            return
        self.overrides.pop(basket.id, None)
        engine.save_overrides(OVERRIDES_PATH, self.overrides)
        self.calculate()

    def edit_strategy_self_mapping(self, strategy_id: str) -> None:
        if self.result is None:
            return
        close = self.venue_by_id.get(strategy_id)
        if close is None:
            QMessageBox.information(self, "未选中", "请先选择一笔国内外碎单自平。")
            return
        override = self.overrides.get(close.id, {})
        open_ids = set(override.get("open_trade_ids") or [item.trade_id for item in close.ib_open])
        close_ids = set(override.get("close_trade_ids") or [item.trade_id for item in close.ib_close])
        dialog = IbMappingDialog(close, self.result.ib_trades, open_ids, close_ids, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        selected_open = dialog.selected_ids(dialog.open_table)
        selected_close = dialog.selected_ids(dialog.close_table)
        protected_open = {
            str(trade_id)
            for basket in self.result.baskets
            for trade_id in (self.overrides.get(basket.id, {}).get("open_trade_ids") or [])
        }
        protected_close = {
            str(trade_id)
            for basket in self.result.baskets
            for trade_id in (self.overrides.get(basket.id, {}).get("close_trade_ids") or [])
        }
        open_conflicts = engine._selection_conflicts(selected_open, protected_open)
        close_conflicts = engine._selection_conflicts(selected_close, protected_close)
        if open_conflicts or close_conflicts:
            QMessageBox.warning(
                self,
                "篮子成交受保护",
                "所选成交已被篮子人工映射占用，不能重复分配给碎单自平。\n"
                + (f"开仓冲突：{', '.join(sorted(open_conflicts))}\n" if open_conflicts else "")
                + (f"平仓冲突：{', '.join(sorted(close_conflicts))}" if close_conflicts else ""),
            )
            return
        self.overrides[close.id] = {
            "open_trade_ids": selected_open,
            "close_trade_ids": selected_close,
        }
        engine.save_overrides(OVERRIDES_PATH, self.overrides)
        self.calculate()

    def clear_strategy_self_mapping(self, strategy_id: str) -> None:
        if strategy_id not in self.overrides:
            return
        self.overrides.pop(strategy_id, None)
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
        strategy_rows = engine.strategy_self_summary_rows(self.result)
        exported = [Path(path)]
        if strategy_rows:
            strategy_path = Path(path).with_name(f"{Path(path).stem}_碎单自平.csv")
            with strategy_path.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(strategy_rows[0]))
                writer.writeheader()
                writer.writerows(strategy_rows)
            exported.append(strategy_path)
        self.status_label.setText("已导出：" + "；".join(str(item) for item in exported))


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
        QTableWidget { background: #ffffff; alternate-background-color: #f8fbff; border: 1px solid #dbe4f0; border-radius: 8px; gridline-color: #edf2f7; selection-background-color: #dbeafe; selection-color: #111827; font-size: 13px; }
        QTableWidget::item { padding: 3px 6px; }
        QHeaderView::section { background: #f8fafc; color: #374151; padding: 5px 6px; border: none; border-bottom: 1px solid #e5e7eb; border-right: 1px solid #eef2f7; }
        QPushButton { background: #ffffff; border: 1px solid #cfd8e3; border-radius: 8px; padding: 5px 12px; }
        QPushButton:hover { background: #f3f7ff; border-color: #b7c7da; }
        QLineEdit, QDoubleSpinBox { background: #ffffff; border: 1px solid #cfd8e3; border-radius: 6px; padding: 4px 6px; }
        QLabel#sourceHint { color: #6b7280; font-size: 12px; }
        QLabel#calculationGuide { color: #374151; font-size: 13px; line-height: 1.5; background: #f8fafc; border: 1px solid #dbe4f0; border-radius: 8px; padding: 10px; }
        QLabel#summaryKey { color: #6b7280; font-size: 10px; }
        QLabel#summaryKeyAccent { color: #c2410c; font-size: 10px; font-weight: 600; }
        QLabel#summaryValue { color: #111827; font-weight: 600; }
        QLabel#summaryValueAccent { color: #9a3412; font-weight: 700; }
        QLabel#summaryValuePositive { color: #14532d; font-weight: 700; }
        QLabel#summaryValueNegative { color: #991b1b; font-weight: 700; }
        QWidget#summaryField { background: #ffffff; border: 1px solid #e5eaf2; border-radius: 6px; }
        QWidget#summaryFieldAccent { background: #fff7ed; border: 1px solid #fdba74; border-radius: 6px; }
        QWidget#summaryFieldPositive { background: #ecfdf3; border: 1px solid #86efac; border-radius: 6px; }
        QWidget#summaryFieldNegative { background: #fef2f2; border: 1px solid #fca5a5; border-radius: 6px; }
        QWidget#fxMetricCard { background: #f8fafc; border: 1px solid #e5eaf2; border-radius: 8px; }
        QWidget#fxMetricCardAccent { background: #eff6ff; border: 1px solid #93c5fd; border-radius: 8px; }
        QLabel#fxMetricKey { color: #6b7280; font-size: 10px; }
        QLabel#fxMetricKeyAccent { color: #1d4ed8; font-size: 10px; font-weight: 600; }
        QLabel#fxMetricValue { color: #111827; font-size: 15px; font-weight: 700; }
        QLabel#fxMetricValueAccent { color: #1d4ed8; font-size: 15px; font-weight: 700; }
        """
    )


def preferred_ui_font_family(
    platform_name: str | None = None,
    available_families: set[str] | None = None,
) -> str:
    platform_value = platform_name or sys.platform
    families = available_families
    if families is None:
        families = set(QFontDatabase().families())
    normalized = {name.casefold(): name for name in families}
    if platform_value.startswith("win"):
        candidates = (
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "Microsoft JhengHei UI",
            "Segoe UI",
        )
    elif platform_value == "darwin":
        candidates = ("PingFang SC", "Hiragino Sans GB", "Arial Unicode MS")
    else:
        candidates = ("Noto Sans CJK SC", "Noto Sans SC", "WenQuanYi Micro Hei", "DejaVu Sans")
    for candidate in candidates:
        installed = normalized.get(candidate.casefold())
        if installed:
            return installed
    return ""


def configure_application_font(app: QApplication) -> str:
    family = preferred_ui_font_family()
    font = QFont(family) if family else QFont(app.font())
    font.setPointSize(10 if sys.platform.startswith("win") else 11)
    font.setStyleStrategy(QFont.PreferAntialias | QFont.PreferQuality)
    if hasattr(QFont, "PreferFullHinting") and sys.platform.startswith("win"):
        font.setHintingPreference(QFont.PreferFullHinting)
    elif hasattr(QFont, "PreferVerticalHinting"):
        font.setHintingPreference(QFont.PreferVerticalHinting)
    app.setFont(font)
    return family or font.family()


def main() -> int:
    os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    os.environ.setdefault("QT_AUTO_SCREEN_SCALE_FACTOR", "1")
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    if hasattr(Qt, "AA_EnableHighDpiScaling"):
        QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    if hasattr(Qt, "AA_UseHighDpiPixmaps"):
        QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps, True)
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(True)
    app.setApplicationName("ETF 赎回收益计算器")
    apply_light_theme(app)
    configure_application_font(app)
    window = MainWindow()
    app.aboutToQuit.connect(window.shutdown)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    # shutdown() has already stopped timers, disconnected live feeds and asked
    # every worker to cancel.  Avoid Python/Qt interpreter teardown waiting on
    # a QThread that is still returning from a bounded network system call.
    os._exit(int(main()))
