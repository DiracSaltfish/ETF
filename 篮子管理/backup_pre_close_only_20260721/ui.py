from __future__ import annotations

import traceback
from datetime import datetime

from PyQt5.QtCore import QObject, QRunnable, Qt, QThreadPool, pyqtSignal
from PyQt5.QtGui import QColor, QCursor, QFont
from PyQt5.QtWidgets import (
    QAction,
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from basket_loader import load_basket_document
from basket_planner import build_component_target_basket, default_target_xop_shares
from basket_models import BasketDocument, ConnectionSettings, OrderMonitorRecord, PortfolioPosition, ReconciliationRow, SymbolMarketState
from config_store import load_config, save_config
from ib_service import (
    cancel_monitor_orders,
    load_market_states,
    load_positions,
    place_component_basket_orders,
    place_single_symbol_order,
    refresh_order_monitor,
    test_connection,
)
from reconcile import reconcile_basket


def fmt_qty(value: float) -> str:
    return f"{value:,.0f}" if abs(value - round(value)) < 1e-9 else f"{value:,.4f}"


def fmt_money(value: float) -> str:
    return f"{value:,.2f}"


BASE_SYMBOL = "XOP"


class WorkerSignals(QObject):
    result = pyqtSignal(object)
    error = pyqtSignal(str)
    finished = pyqtSignal()


class Worker(QRunnable):
    def __init__(self, fn, *args, **kwargs) -> None:
        super().__init__()
        self.fn = fn
        self.args = args
        self.kwargs = kwargs
        self.signals = WorkerSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args, **self.kwargs)
        except Exception:
            self.signals.error.emit(traceback.format_exc())
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()


class MetricCard(QFrame):
    def __init__(self, title: str, accent: str) -> None:
        super().__init__()
        self.setObjectName("metricCard")
        self.setStyleSheet(f"QFrame#metricCard {{ border-top: 5px solid {accent}; }}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(6)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("metricTitle")
        self.value_label = QLabel("--")
        self.value_label.setObjectName("metricValue")
        self.note_label = QLabel("")
        self.note_label.setObjectName("mutedHint")
        self.note_label.setWordWrap(True)
        layout.addWidget(self.title_label)
        layout.addWidget(self.value_label)
        layout.addWidget(self.note_label)
        layout.addStretch(1)

    def set_value(self, value: str, note: str = "") -> None:
        self.value_label.setText(value)
        self.note_label.setText(note)


class PathPicker(QWidget):
    def __init__(self, title: str) -> None:
        super().__init__()
        self.title = title
        self.edit = QLineEdit()
        self.button = QPushButton("浏览")
        self.button.setObjectName("secondaryButton")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        layout.addWidget(self.edit, 1)
        layout.addWidget(self.button)
        self.button.clicked.connect(self.pick_file)

    def pick_file(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            self.title,
            self.edit.text(),
            "Basket Files (*.csv *.xlsx *.xls)",
        )
        if path:
            self.edit.setText(path)

    def value(self) -> str:
        return self.edit.text().strip()

    def set_value(self, value: str) -> None:
        self.edit.setText(value)


class SettingsDialog(QDialog):
    def __init__(self, config: dict[str, object], known_accounts: tuple[str, ...], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("设置")
        self.resize(560, 460)

        self.host_edit = QLineEdit(str(config.get("host") or "127.0.0.1"))
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setValue(int(config.get("port") or 7496))
        self.client_id_spin = QSpinBox()
        self.client_id_spin.setRange(1, 999999)
        self.client_id_spin.setValue(int(config.get("client_id") or 9701))
        self.account_combo = QComboBox()
        self.account_combo.setEditable(True)
        if known_accounts:
            self.account_combo.addItems(known_accounts)
        current_account = str(config.get("account") or "")
        if current_account and self.account_combo.findText(current_account) < 0:
            self.account_combo.addItem(current_account)
        self.account_combo.setCurrentText(current_account)

        ib_form = QFormLayout()
        ib_form.addRow("Host", self.host_edit)
        ib_form.addRow("Port", self.port_spin)
        ib_form.addRow("Client ID", self.client_id_spin)
        ib_form.addRow("Account", self.account_combo)

        ib_card = QFrame()
        ib_card.setObjectName("panelCard")
        ib_layout = QVBoxLayout(ib_card)
        ib_layout.setContentsMargins(16, 16, 16, 16)
        ib_layout.setSpacing(10)
        ib_title = QLabel("IBKR 连接")
        ib_title.setObjectName("sectionTitle")
        ib_hint = QLabel("低频配置放到这里，主界面只保留连接状态和操作按钮。")
        ib_hint.setObjectName("mutedHint")
        ib_hint.setWordWrap(True)
        ib_layout.addWidget(ib_title)
        ib_layout.addWidget(ib_hint)
        ib_layout.addLayout(ib_form)

        self.basket_path_picker = PathPicker("选择篮子文件")
        self.basket_path_picker.set_value(str(config.get("basket_path") or ""))

        basket_card = QFrame()
        basket_card.setObjectName("panelCard")
        basket_layout = QVBoxLayout(basket_card)
        basket_layout.setContentsMargins(16, 16, 16, 16)
        basket_layout.setSpacing(10)
        basket_title = QLabel("篮子文件")
        basket_title.setObjectName("sectionTitle")
        basket_hint = QLabel("低频调整的篮子导入放到这里，主界面只保留执行和监控。")
        basket_hint.setObjectName("mutedHint")
        basket_hint.setWordWrap(True)
        basket_layout.addWidget(basket_title)
        basket_layout.addWidget(basket_hint)
        basket_layout.addWidget(self.basket_path_picker)

        self.tif_combo = QComboBox()
        self.tif_combo.addItems(["DAY", "GTC"])
        self.tif_combo.setCurrentText(str(config.get("tif") or "DAY"))
        self.outside_rth_check = QCheckBox("允许 Outside RTH")
        self.outside_rth_check.setChecked(bool(config.get("outside_rth")))

        order_form = QFormLayout()
        order_form.addRow("TIF", self.tif_combo)
        order_form.addRow("", self.outside_rth_check)

        order_card = QFrame()
        order_card.setObjectName("panelCard")
        order_layout = QVBoxLayout(order_card)
        order_layout.setContentsMargins(16, 16, 16, 16)
        order_layout.setSpacing(10)
        order_title = QLabel("委托通用参数")
        order_title.setObjectName("sectionTitle")
        order_hint = QLabel("这里保留低频参数；成分股买卖方向和 XOP 手动价格在主界面上控制。")
        order_hint.setObjectName("mutedHint")
        order_hint.setWordWrap(True)
        order_layout.addWidget(order_title)
        order_layout.addWidget(order_hint)
        order_layout.addLayout(order_form)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(ib_card)
        layout.addWidget(basket_card)
        layout.addWidget(order_card)
        layout.addWidget(buttons)

    def values(self) -> dict[str, object]:
        return {
            "host": self.host_edit.text().strip() or "127.0.0.1",
            "port": self.port_spin.value(),
            "client_id": self.client_id_spin.value(),
            "account": self.account_combo.currentText().strip(),
            "basket_path": self.basket_path_picker.value(),
            "tif": self.tif_combo.currentText(),
            "outside_rth": self.outside_rth_check.isChecked(),
        }


def configured_table() -> QTableWidget:
    table = QTableWidget()
    table.setAlternatingRowColors(True)
    table.setEditTriggers(QAbstractItemView.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectRows)
    table.setSelectionMode(QAbstractItemView.SingleSelection)
    table.setWordWrap(False)
    table.setTextElideMode(Qt.ElideRight)
    table.verticalHeader().setVisible(False)
    table.verticalHeader().setDefaultSectionSize(34)
    table.verticalHeader().setMinimumSectionSize(28)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
    table.setShowGrid(False)
    table.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
    return table


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("IBKR 篮子管理")
        self.resize(1540, 980)
        self.config = load_config()
        self.pool = QThreadPool.globalInstance()
        self.active_workers = 0
        self.snapshot = None
        self.known_accounts: tuple[str, ...] = ()
        self.basket: BasketDocument | None = None
        self.component_target_basket: BasketDocument | None = None
        self.positions: tuple[PortfolioPosition, ...] = ()
        self.market_states: tuple[SymbolMarketState, ...] = ()
        self.reconciliation_rows: tuple[ReconciliationRow, ...] = ()
        self.monitor_records: tuple[OrderMonitorRecord, ...] = ()
        self.monitor_batch_seq = 0
        self._build_ui()
        self._load_saved_state()

    def _build_ui(self) -> None:
        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(18, 18, 18, 18)
        central_layout.setSpacing(10)
        self.setup_menu()
        central_layout.addWidget(self._build_action_bar())

        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_sidebar())
        splitter.addWidget(self._build_main_area())
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([330, 1170])
        central_layout.addWidget(splitter, 1)
        self.setCentralWidget(central)

    def _build_action_bar(self) -> QWidget:
        card = QFrame()
        card.setObjectName("panelCard")
        layout = QHBoxLayout(card)
        layout.setContentsMargins(18, 12, 18, 12)
        layout.setSpacing(12)
        self.status_chip = QLabel("未连接")
        self.status_chip.setObjectName("statusChip")
        self.status_chip.setAlignment(Qt.AlignCenter)
        self.status_chip.setMinimumWidth(112)
        self.connection_summary = QLabel("尚未探测 TWS。请在菜单栏“设置”中维护连接参数。")
        self.connection_summary.setObjectName("mutedHint")
        self.connection_summary.setWordWrap(False)

        buttons = QHBoxLayout()
        self.connect_button = QPushButton("连接 TWS")
        self.connect_button.setObjectName("secondaryButton")
        self.refresh_holdings_button = QPushButton("刷新持仓")
        self.refresh_market_button = QPushButton("刷新券源")
        self.refresh_market_button.setObjectName("secondaryButton")
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.refresh_holdings_button)
        buttons.addWidget(self.refresh_market_button)

        layout.addWidget(self.status_chip, 0, Qt.AlignVCenter)
        layout.addWidget(self.connection_summary, 1)
        layout.addLayout(buttons)

        self.connect_button.clicked.connect(self.probe_connection)
        self.refresh_holdings_button.clicked.connect(self.refresh_holdings)
        self.refresh_market_button.clicked.connect(self.refresh_market_states)
        return card

    def _build_sidebar(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)
        layout.addWidget(self._build_order_card())
        layout.addStretch(1)
        return container

    def _build_order_card(self) -> QWidget:
        card = QFrame()
        card.setObjectName("panelCard")
        layout = QVBoxLayout(card)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(12)
        title = QLabel("订单执行")
        title.setObjectName("sectionTitle")

        component_box = QFrame()
        component_box.setObjectName("noteCard")
        component_layout = QVBoxLayout(component_box)
        component_layout.setContentsMargins(14, 14, 14, 14)
        component_layout.setSpacing(10)
        component_title = QLabel("成分股篮子")
        component_title.setObjectName("sectionTitle")
        component_hint = QLabel("按目标 XOP 股数折算成分股目标仓位，只对成分股做补差下单。")
        component_hint.setObjectName("mutedHint")
        component_hint.setWordWrap(True)
        self.component_target_xop_spin = QSpinBox()
        self.component_target_xop_spin.setRange(1, 1_000_000)
        self.component_target_xop_spin.setValue(990)
        target_form = QFormLayout()
        target_form.addRow("目标 XOP 股数", self.component_target_xop_spin)
        self.component_mode_combo = QComboBox()
        self.component_mode_combo.addItems(["市价", "盘口对手价"])
        component_buttons = QHBoxLayout()
        self.buy_components_button = QPushButton("买回到目标")
        self.buy_components_button.setObjectName("secondaryButton")
        self.sell_components_button = QPushButton("卖出到目标（融券）")
        self.sell_components_button.setObjectName("dangerButton")
        component_buttons.addWidget(self.buy_components_button)
        component_buttons.addWidget(self.sell_components_button)
        self.component_note = QLabel("成分股篮子执行前会做账户和券源校验。")
        self.component_note.setObjectName("mutedHint")
        self.component_note.setWordWrap(True)
        component_layout.addWidget(component_title)
        component_layout.addWidget(component_hint)
        component_layout.addLayout(target_form)
        component_layout.addWidget(self.component_mode_combo)
        component_layout.addLayout(component_buttons)
        component_layout.addWidget(self.component_note)

        xop_box = QFrame()
        xop_box.setObjectName("noteCard")
        xop_layout = QVBoxLayout(xop_box)
        xop_layout.setContentsMargins(14, 14, 14, 14)
        xop_layout.setSpacing(10)
        xop_title = QLabel(f"{BASE_SYMBOL} 手动下单")
        xop_title.setObjectName("sectionTitle")
        xop_hint = QLabel(f"{BASE_SYMBOL} 可独立设置方向、数量、限价/市价，不再固定一次下 990 股。")
        xop_hint.setObjectName("mutedHint")
        xop_hint.setWordWrap(True)
        self.xop_side_combo = QComboBox()
        self.xop_side_combo.addItems(["BUY", "SELL"])
        self.xop_order_type_combo = QComboBox()
        self.xop_order_type_combo.addItems(["MKT", "LMT"])
        self.xop_qty_spin = QSpinBox()
        self.xop_qty_spin.setRange(1, 1_000_000)
        self.xop_qty_spin.setValue(990)
        self.xop_price_spin = QDoubleSpinBox()
        self.xop_price_spin.setDecimals(4)
        self.xop_price_spin.setRange(0.0, 10000.0)
        self.xop_price_spin.setSingleStep(0.01)
        self.xop_price_spin.setEnabled(False)
        xop_form = QFormLayout()
        xop_form.addRow("方向", self.xop_side_combo)
        xop_form.addRow("价格模式", self.xop_order_type_combo)
        xop_form.addRow("数量", self.xop_qty_spin)
        xop_form.addRow("限价", self.xop_price_spin)
        self.submit_xop_button = QPushButton(f"提交 {BASE_SYMBOL} 订单")
        self.submit_xop_button.setObjectName("secondaryButton")
        self.xop_note = QLabel("手动单票执行，适合分批买卖 XOP。")
        self.xop_note.setObjectName("mutedHint")
        self.xop_note.setWordWrap(True)
        xop_layout.addWidget(xop_title)
        xop_layout.addWidget(xop_hint)
        xop_layout.addLayout(xop_form)
        xop_layout.addWidget(self.submit_xop_button)
        xop_layout.addWidget(self.xop_note)

        layout.addWidget(title)
        layout.addWidget(component_box)
        layout.addWidget(xop_box)
        self.buy_components_button.clicked.connect(lambda: self.submit_component_basket("BUY"))
        self.sell_components_button.clicked.connect(lambda: self.submit_component_basket("SELL"))
        self.submit_xop_button.clicked.connect(self.submit_xop_order)
        self.xop_order_type_combo.currentTextChanged.connect(self.update_xop_price_input_state)
        self.component_target_xop_spin.valueChanged.connect(self.refresh_views)
        return card

    def _build_main_area(self) -> QWidget:
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(14)

        self.recon_table = configured_table()
        self.portfolio_table = configured_table()
        self.monitor_table = configured_table()
        self.monitor_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        self.log_output.setMaximumBlockCount(1200)
        self.log_output.setFont(QFont("Menlo", 11))

        monitor_widget = QWidget()
        monitor_layout = QVBoxLayout(monitor_widget)
        monitor_layout.setContentsMargins(0, 0, 0, 0)
        monitor_layout.setSpacing(10)
        monitor_bar = QHBoxLayout()
        self.refresh_monitor_button = QPushButton("刷新监控")
        self.refresh_monitor_button.setObjectName("secondaryButton")
        self.cancel_monitor_button = QPushButton("撤单选中")
        self.cancel_monitor_button.setObjectName("dangerButton")
        self.resubmit_symbol_label = QLabel("补单对象：--")
        self.resubmit_symbol_label.setObjectName("mutedHint")
        self.resubmit_mode_combo = QComboBox()
        self.resubmit_mode_combo.addItems(["市价", "盘口对手价"])
        self.resubmit_qty_spin = QSpinBox()
        self.resubmit_qty_spin.setRange(1, 1_000_000)
        self.resubmit_qty_spin.setValue(1)
        self.resubmit_button = QPushButton("补单选中标的")
        self.resubmit_button.setObjectName("secondaryButton")
        self.monitor_summary = QLabel("暂无已跟踪订单。")
        self.monitor_summary.setObjectName("mutedHint")
        self.monitor_summary.setWordWrap(True)
        monitor_bar.addWidget(self.refresh_monitor_button)
        monitor_bar.addWidget(self.cancel_monitor_button)
        monitor_bar.addWidget(self.resubmit_symbol_label)
        monitor_bar.addWidget(self.resubmit_mode_combo)
        monitor_bar.addWidget(self.resubmit_qty_spin)
        monitor_bar.addWidget(self.resubmit_button)
        monitor_bar.addWidget(self.monitor_summary, 1)
        monitor_layout.addLayout(monitor_bar)
        monitor_layout.addWidget(self.monitor_table, 1)

        tabs = QTabWidget()
        tabs.addTab(self.recon_table, "篮子核对")
        tabs.addTab(self.portfolio_table, "IB 持仓")
        tabs.addTab(monitor_widget, "下单过程监控")
        tabs.addTab(self.log_output, "操作日志")
        self.tabs = tabs
        self.refresh_monitor_button.clicked.connect(self.refresh_order_monitor_tab)
        self.cancel_monitor_button.clicked.connect(self.cancel_selected_monitor_orders)
        self.resubmit_button.clicked.connect(self.resubmit_selected_monitor_order)
        self.recon_table.itemClicked.connect(self.show_recon_note_tooltip)
        self.monitor_table.itemSelectionChanged.connect(self.on_monitor_selection_changed)

        layout.addWidget(tabs, 1)
        return container

    def _load_saved_state(self) -> None:
        basket_path = str(self.config.get("basket_path") or "").strip()
        if basket_path:
            try:
                self.basket = load_basket_document(basket_path)
            except Exception as exc:
                self.append_log(f"启动时导入篮子失败: {exc}")
            else:
                self.append_log(f"已加载默认篮子: {self.basket.path.name}")
                base_target = default_target_xop_shares(self.basket, base_symbol=BASE_SYMBOL)
                if base_target > 0:
                    self.component_target_xop_spin.setValue(base_target)
        self.refresh_config_summaries()
        self.refresh_views()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.persist_config()
        super().closeEvent(event)

    def persist_config(self) -> None:
        save_config(self.config)

    def gather_connection_settings(self) -> ConnectionSettings:
        return ConnectionSettings(
            host=str(self.config.get("host") or "127.0.0.1"),
            port=int(self.config.get("port") or 7496),
            client_id=int(self.config.get("client_id") or 9701),
            account=str(self.config.get("account") or "").strip(),
        )

    def current_common_order_settings(self) -> tuple[str, bool]:
        return (
            str(self.config.get("tif") or "DAY"),
            bool(self.config.get("outside_rth")),
        )

    def current_account(self) -> str:
        return str(self.config.get("account") or "").strip()

    def component_rows(self):
        if not self.component_target_basket:
            return ()
        return tuple(item for item in self.component_target_basket.rows if item.quantity > 0)

    def xop_market_price(self) -> float:
        for state in self.market_states:
            if state.symbol.upper() == BASE_SYMBOL and state.market_price:
                return state.market_price
        for row in self.reconciliation_rows:
            if row.item.symbol.upper() == BASE_SYMBOL and row.market_price:
                return row.market_price
        return 0.0

    def component_sell_summary(self) -> tuple[int, int, int, list[tuple[str, float, bool]]]:
        sell_rows = [row for row in self.reconciliation_rows if row.item.action == "SELL" and row.delta_to_target < -1e-9]
        if not sell_rows:
            return 0, 0, 0, []
        state_map = {item.symbol: item for item in self.market_states}
        ready_count = 0
        covered_qty = 0
        blockers: list[tuple[str, float, bool]] = []
        for row in sell_rows:
            required_qty = max(0.0, -row.delta_to_target)
            state = state_map.get(row.item.symbol)
            shortable_shares = state.shortable_shares if state else None
            total_capacity = max(row.long_inventory, 0.0) + (max(shortable_shares, 0.0) if shortable_shares is not None else 0.0)
            covered_qty += min(int(round(total_capacity)), int(round(required_qty)))
            if total_capacity + 1e-9 >= required_qty:
                ready_count += 1
            else:
                blockers.append((row.item.symbol, max(0.0, required_qty - total_capacity), shortable_shares is None))
        return len(sell_rows), ready_count, covered_qty, blockers

    def setup_menu(self) -> None:
        settings_menu = self.menuBar().addMenu("设置")
        connection_action = QAction("连接 / 篮子 / 委托设置", self)
        connection_action.triggered.connect(self.open_settings)
        settings_menu.addAction(connection_action)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.config, self.known_accounts, self)
        if dialog.exec_() != QDialog.Accepted:
            return
        new_values = dialog.values()
        new_basket_path = str(new_values.get("basket_path") or "").strip()
        previous_basket_path = str(self.config.get("basket_path") or "").strip()
        new_basket = None
        if new_basket_path:
            try:
                new_basket = load_basket_document(new_basket_path)
            except Exception as exc:
                QMessageBox.critical(self, "篮子加载失败", str(exc))
                self.append_log(f"设置中的篮子加载失败: {exc}")
                return

        self.config.update(new_values)
        self.basket = new_basket
        save_config(self.config)
        basket_changed = new_basket_path != previous_basket_path
        if basket_changed:
            if self.basket:
                self.append_log(f"已在设置中载入篮子 {self.basket.path.name}，共 {self.basket.row_count} 行。")
                base_target = default_target_xop_shares(self.basket, base_symbol=BASE_SYMBOL)
                if base_target > 0:
                    self.component_target_xop_spin.setValue(base_target)
            elif previous_basket_path:
                self.append_log("已在设置中清空篮子配置。")
        self.refresh_config_summaries()
        if basket_changed and not self.basket:
            self.market_states = ()
            self.refresh_views()
            return
        if basket_changed and self.snapshot:
            self.append_log("篮子配置已变更，自动刷新券源和行情。")
            self.refresh_market_states()
            return
        self.refresh_views()

    def refresh_config_summaries(self) -> None:
        settings = self.gather_connection_settings()
        if self.snapshot:
            account = self.snapshot.active_account or self.current_account() or "未选账户"
            shortable_count = sum(1 for item in self.market_states if item.shortable_shares is not None)
            self.connection_summary.setText(
                f"{settings.host}:{settings.port} · clientId {settings.client_id} · "
                f"account {account} · 股票持仓 {len(self.positions)} 行 · 券源 {shortable_count}/{len(self.market_states)} 行 · {self.snapshot.server_time}"
            )
        else:
            account = self.current_account() or "未设置"
            self.connection_summary.setText(
                f"{settings.host}:{settings.port} · clientId {settings.client_id} · account {account} · 未连接"
            )

    def append_log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_output.appendPlainText(f"[{stamp}] {message}")

    def confirm_order_submission(self, title: str, preview_text: str, final_text: str) -> bool:
        first_answer = QMessageBox.question(
            self,
            title,
            preview_text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if first_answer != QMessageBox.Yes:
            return False
        second_answer = QMessageBox.question(
            self,
            f"{title} - 最终确认",
            final_text,
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        return second_answer == QMessageBox.Yes

    def set_busy(self, busy: bool, message: str = "") -> None:
        if busy:
            self.active_workers += 1
        else:
            self.active_workers = max(0, self.active_workers - 1)
        active = self.active_workers > 0
        self.connect_button.setEnabled(not active)
        self.refresh_holdings_button.setEnabled(not active)
        self.refresh_market_button.setEnabled(not active and self.basket is not None)
        self.refresh_monitor_button.setEnabled(not active and bool(self.monitor_records) and self.snapshot is not None)
        self.cancel_monitor_button.setEnabled(not active and bool(self.selected_monitor_records()))
        self.resubmit_button.setEnabled(not active and self.can_resubmit_selected_monitor_order())
        if message:
            self.status_chip.setText(message)
        elif active:
            self.status_chip.setText("处理中")
        elif self.snapshot:
            account = self.snapshot.active_account or "未选账户"
            self.status_chip.setText(f"已连接 · {account}")
        else:
            self.status_chip.setText("未连接")
        self.update_execution_controls()

    def start_worker(self, fn, on_result, *, busy_message: str, log_message: str) -> None:
        worker = Worker(fn)
        worker.signals.result.connect(on_result)
        worker.signals.error.connect(self.on_worker_error)
        worker.signals.finished.connect(lambda: self.set_busy(False))
        self.set_busy(True, busy_message)
        self.append_log(log_message)
        self.pool.start(worker)

    def probe_connection(self) -> None:
        self.persist_config()
        settings = self.gather_connection_settings()
        self.start_worker(
            lambda: test_connection(settings),
            self.on_probe_finished,
            busy_message="连接测试中",
            log_message=f"开始连接测试 {settings.host}:{settings.port} clientId={settings.client_id}",
        )

    def refresh_holdings(self) -> None:
        self.persist_config()
        settings = self.gather_connection_settings()
        self.start_worker(
            lambda: load_positions(settings),
            self.on_holdings_finished,
            busy_message="刷新持仓中",
            log_message="开始刷新 IB 股票持仓",
        )

    def refresh_market_states(self) -> None:
        if not self.basket:
            QMessageBox.information(self, "无需刷新", "请先在设置中配置篮子文件。")
            return
        self.persist_config()
        settings = self.gather_connection_settings()
        symbols = tuple(item.symbol for item in self.basket.rows)
        self.start_worker(
            lambda: load_market_states(settings, symbols),
            self.on_market_states_finished,
            busy_message="刷新券源中",
            log_message="开始刷新篮子行情和可融券数据",
        )

    def on_probe_finished(self, snapshot) -> None:
        self.snapshot = snapshot
        self.remember_accounts(snapshot.managed_accounts, snapshot.active_account)
        account = snapshot.active_account or "未选账户"
        self.append_log(f"连接成功，当前账户: {account}")
        self.refresh_config_summaries()
        self.refresh_views()

    def on_holdings_finished(self, payload) -> None:
        snapshot, positions = payload
        self.snapshot = snapshot
        self.positions = positions
        self.remember_accounts(snapshot.managed_accounts, snapshot.active_account)
        self.append_log(f"已刷新股票持仓 {len(positions)} 行")
        self.refresh_config_summaries()
        self.refresh_views()

    def on_market_states_finished(self, payload) -> None:
        snapshot, market_states = payload
        self.snapshot = snapshot
        self.market_states = market_states
        self.remember_accounts(snapshot.managed_accounts, snapshot.active_account)
        self.append_log(f"已刷新篮子行情/券源 {len(market_states)} 行")
        self.refresh_config_summaries()
        self.refresh_views()

    def remember_accounts(self, accounts: tuple[str, ...], active_account: str) -> None:
        self.known_accounts = accounts
        if active_account:
            self.config["account"] = active_account
            save_config(self.config)

    def on_worker_error(self, text: str) -> None:
        self.append_log("任务失败")
        self.append_log(text.strip())
        QMessageBox.critical(self, "执行失败", text)

    def refresh_views(self) -> None:
        if self.basket:
            self.component_target_basket = build_component_target_basket(
                self.basket,
                target_xop_shares=self.component_target_xop_spin.value(),
                base_symbol=BASE_SYMBOL,
            )
            self.reconciliation_rows = reconcile_basket(self.component_target_basket, self.positions, self.market_states)
        else:
            self.component_target_basket = None
            self.reconciliation_rows = ()
        self.refresh_config_summaries()
        self.populate_recon_table()
        self.populate_portfolio_table()
        self.populate_monitor_table()
        self.update_xop_price_input_state()
        self.update_execution_controls()

    def planned_component_orders(self, action: str) -> tuple:
        action = action.upper()
        if action == "SELL":
            return tuple(
                item
                for item in (
                    self._delta_item_from_row(row, "SELL", -row.delta_to_target)
                    for row in self.reconciliation_rows
                    if row.item.action == "SELL" and row.delta_to_target < -1e-9
                )
                if item is not None
            )
        if action == "BUY":
            return tuple(
                item
                for item in (
                    self._delta_item_from_row(row, "BUY", row.delta_to_target)
                    for row in self.reconciliation_rows
                    if row.item.action == "SELL" and row.delta_to_target > 1e-9
                )
                if item is not None
            )
        return ()

    @staticmethod
    def _delta_item_from_row(row: ReconciliationRow, action: str, quantity: float):
        rounded = int(round(quantity))
        if rounded <= 0:
            return None
        return row.item.__class__(
            symbol=row.item.symbol,
            action=action,
            quantity=rounded,
            name=row.item.name,
            source_sheet=row.item.source_sheet,
            source_row=row.item.source_row,
        )

    def populate_recon_table(self) -> None:
        headers = [
            "代码",
            "方向",
            "篮子数量",
            "目标净仓",
            "当前净仓",
            "到目标差额",
            "多头库存",
            "可融数量",
            "执行缺口",
            "目标匹配",
            "执行状态",
            "市价",
            "市值",
            "账户",
            "说明",
        ]
        self.recon_table.setColumnCount(len(headers))
        self.recon_table.setHorizontalHeaderLabels(headers)
        self.recon_table.horizontalHeader().setStretchLastSection(False)
        self.recon_table.horizontalHeader().setSectionResizeMode(14, QHeaderView.Fixed)
        self.recon_table.setColumnWidth(14, 76)
        self.recon_table.setRowCount(len(self.reconciliation_rows))
        for row_index, row in enumerate(self.reconciliation_rows):
            values = [
                row.item.symbol,
                row.item.action,
                fmt_qty(row.item.quantity),
                fmt_qty(row.target_position),
                fmt_qty(row.current_position),
                f"{row.delta_to_target:+,.0f}",
                fmt_qty(row.long_inventory) if row.item.action == "SELL" else "--",
                fmt_qty(row.shortable_shares) if row.item.action == "SELL" and row.shortable_shares is not None else ("待查询" if row.item.action == "SELL" else "--"),
                fmt_qty(row.execution_shortfall) if row.item.action == "SELL" else "--",
                row.target_status,
                row.sell_status,
                fmt_money(row.market_price) if row.market_price else "--",
                fmt_money(row.market_value) if row.market_value else "--",
                row.account or "--",
                "详情" if row.note else "--",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col in {2, 3, 4, 5, 6, 7, 8, 11, 12}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 14 and row.note:
                    item.setTextAlignment(Qt.AlignCenter)
                    item.setForeground(QColor("#0f6c74"))
                    item.setToolTip(row.note)
                    item.setData(Qt.UserRole, row.note)
                self.recon_table.setItem(row_index, col, item)

            target_item = self.recon_table.item(row_index, 9)
            sell_item = self.recon_table.item(row_index, 10)
            if row.target_matched:
                target_item.setBackground(QColor("#d8ecdf"))
            else:
                target_item.setBackground(QColor("#f5e6c9"))
            if row.item.action == "SELL":
                sell_item.setBackground(QColor("#d8ecdf") if row.sell_ready else QColor("#f4d8cf"))
            else:
                sell_item.setBackground(QColor("#e6eaef"))

    def show_recon_note_tooltip(self, item: QTableWidgetItem) -> None:
        if item.column() != 14:
            return
        note = item.data(Qt.UserRole)
        if note:
            QToolTip.showText(QCursor.pos(), str(note), self.recon_table)

    def populate_portfolio_table(self) -> None:
        headers = ["代码", "数量", "均价", "市价", "市值", "浮盈亏", "已实现", "账户", "交易所"]
        self.portfolio_table.setColumnCount(len(headers))
        self.portfolio_table.setHorizontalHeaderLabels(headers)
        self.portfolio_table.setRowCount(len(self.positions))
        for row_index, position in enumerate(self.positions):
            values = [
                position.symbol,
                fmt_qty(position.quantity),
                fmt_money(position.avg_cost),
                fmt_money(position.market_price),
                fmt_money(position.market_value),
                fmt_money(position.unrealized_pnl),
                fmt_money(position.realized_pnl),
                position.account,
                position.exchange,
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col in {1, 2, 3, 4, 5, 6}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 5:
                    pnl = position.unrealized_pnl
                    if pnl > 0:
                        item.setForeground(QColor("#237a57"))
                    elif pnl < 0:
                        item.setForeground(QColor("#b84e34"))
                self.portfolio_table.setItem(row_index, col, item)
        self.portfolio_table.resizeRowsToContents()

    def populate_monitor_table(self) -> None:
        headers = [
            "批次",
            "分组",
            "提交时间",
            "代码",
            "方向",
            "数量",
            "类型",
            "限价",
            "最新状态",
            "已成交",
            "剩余",
            "均价",
            "Order ID",
            "Perm ID",
            "最近更新",
            "备注",
        ]
        self.monitor_table.setColumnCount(len(headers))
        self.monitor_table.setHorizontalHeaderLabels(headers)
        self.monitor_table.setRowCount(len(self.monitor_records))
        for row_index, record in enumerate(self.monitor_records):
            values = [
                record.batch_id,
                record.group_label,
                record.submitted_at,
                record.symbol,
                record.action,
                fmt_qty(record.quantity),
                record.order_type,
                fmt_money(record.limit_price) if record.limit_price is not None else "--",
                record.status,
                fmt_qty(record.filled),
                fmt_qty(record.remaining),
                fmt_money(record.avg_fill_price) if record.avg_fill_price else "--",
                str(record.order_id or "--"),
                str(record.perm_id or "--"),
                record.last_update or "--",
                record.note or "",
            ]
            for col, value in enumerate(values):
                item = QTableWidgetItem(str(value))
                if col in {5, 7, 9, 10, 11, 12, 13}:
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                if col == 8:
                    status = record.status.upper()
                    if "FILL" in status:
                        item.setBackground(QColor("#d8ecdf"))
                    elif "CANCEL" in status or "INACTIVE" in status:
                        item.setBackground(QColor("#f4d8cf"))
                    elif "SUBMIT" in status or "PENDING" in status:
                        item.setBackground(QColor("#f5e6c9"))
                self.monitor_table.setItem(row_index, col, item)
        self.monitor_table.resizeRowsToContents()
        if self.monitor_records:
            done_count = sum(1 for record in self.monitor_records if record.status in {"Filled", "Cancelled", "ApiCancelled"})
            self.monitor_summary.setText(
                f"已跟踪 {len(self.monitor_records)} 笔订单，完成 {done_count} 笔。"
            )
        else:
            self.monitor_summary.setText("暂无已跟踪订单。")
        self.on_monitor_selection_changed()

    def selected_monitor_records(self) -> list[OrderMonitorRecord]:
        rows = sorted({index.row() for index in self.monitor_table.selectionModel().selectedRows()})
        result: list[OrderMonitorRecord] = []
        for row in rows:
            if 0 <= row < len(self.monitor_records):
                result.append(self.monitor_records[row])
        return result

    @staticmethod
    def monitor_record_is_active(record: OrderMonitorRecord) -> bool:
        return record.status in {"PendingSubmit", "ApiPending", "PreSubmitted", "Submitted"}

    def can_resubmit_selected_monitor_order(self) -> bool:
        selected = self.selected_monitor_records()
        if len(selected) != 1 or not self.snapshot or not self.current_account():
            return False
        record = selected[0]
        return not self.monitor_record_is_active(record)

    def on_monitor_selection_changed(self) -> None:
        selected = self.selected_monitor_records()
        if len(selected) == 1:
            record = selected[0]
            self.resubmit_symbol_label.setText(f"补单对象：{record.symbol} {record.action}")
            default_qty = int(round(record.remaining)) if record.remaining > 0 else int(record.quantity)
            self.resubmit_qty_spin.setValue(max(1, default_qty))
        elif len(selected) > 1:
            self.resubmit_symbol_label.setText("补单对象：多选，仅支持单标的补单")
        else:
            self.resubmit_symbol_label.setText("补单对象：--")
        active = self.active_workers == 0
        self.cancel_monitor_button.setEnabled(active and bool(selected))
        self.resubmit_button.setEnabled(active and self.can_resubmit_selected_monitor_order())

    def register_monitor_orders(self, orders, group_label: str) -> None:
        self.monitor_batch_seq += 1
        submitted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        batch_id = f"B{self.monitor_batch_seq:03d}"
        new_records = [
            OrderMonitorRecord(
                batch_id=batch_id,
                group_label=group_label,
                submitted_at=submitted_at,
                symbol=order.symbol,
                action=order.action,
                quantity=order.quantity,
                order_type=order.order_type,
                limit_price=order.limit_price,
                order_id=order.order_id,
                perm_id=order.perm_id,
                status=order.status,
                filled=0.0,
                remaining=float(order.quantity),
                avg_fill_price=0.0,
                last_update=submitted_at,
                note="",
            )
            for order in orders
        ]
        self.monitor_records = tuple(new_records) + self.monitor_records
        self.populate_monitor_table()
        self.tabs.setCurrentIndex(2)

    def refresh_order_monitor_tab(self) -> None:
        if not self.monitor_records:
            QMessageBox.information(self, "无需刷新", "当前还没有已跟踪订单。")
            return
        if not self.snapshot or not self.current_account():
            QMessageBox.warning(self, "无法刷新", "请先连接 TWS 并选择账户。")
            return
        settings = self.gather_connection_settings()
        self.start_worker(
            lambda: refresh_order_monitor(settings, self.monitor_records),
            self.on_monitor_refresh_finished,
            busy_message="刷新订单监控中",
            log_message="开始刷新下单过程监控",
        )

    def on_monitor_refresh_finished(self, payload) -> None:
        snapshot, records = payload
        self.snapshot = snapshot
        self.monitor_records = records
        self.append_log(f"已刷新订单监控 {len(records)} 笔")
        self.refresh_views()

    def cancel_selected_monitor_orders(self) -> None:
        selected = self.selected_monitor_records()
        if not selected:
            QMessageBox.information(self, "无需撤单", "请先在监控表中选择一行或多行订单。")
            return
        active_records = [record for record in selected if self.monitor_record_is_active(record)]
        if not active_records:
            QMessageBox.information(self, "无需撤单", "选中的订单当前不是活动状态，无法再发撤单。")
            return
        preview = "\n".join(f"{record.symbol} {record.action} {fmt_qty(record.remaining or record.quantity)}" for record in active_records[:8])
        if len(active_records) > 8:
            preview += f"\n... 其余 {len(active_records) - 8} 笔略"
        answer = QMessageBox.question(
            self,
            "确认撤单",
            f"将对 {len(active_records)} 笔活动订单发送撤单请求：\n\n{preview}\n\n是否继续？",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if answer != QMessageBox.Yes:
            return
        settings = self.gather_connection_settings()
        self.start_worker(
            lambda: cancel_monitor_orders(settings, tuple(active_records)),
            self.on_cancel_monitor_orders_finished,
            busy_message="发送撤单请求中",
            log_message=f"开始对 {len(active_records)} 笔订单发送撤单请求",
        )

    def on_cancel_monitor_orders_finished(self, payload) -> None:
        snapshot, messages = payload
        self.snapshot = snapshot
        summary = "\n".join(f"{symbol}: {message}" for symbol, message in messages)
        self.append_log("撤单请求已发送")
        self.append_log(summary)
        QMessageBox.information(self, "撤单请求已发送", summary)
        self.refresh_order_monitor_tab()

    def resubmit_selected_monitor_order(self) -> None:
        selected = self.selected_monitor_records()
        if len(selected) != 1:
            QMessageBox.information(self, "无法补单", "请在监控表中只选择一笔订单做补单。")
            return
        record = selected[0]
        if self.monitor_record_is_active(record):
            QMessageBox.warning(self, "无法补单", "该订单仍是活动状态。请先撤单并刷新状态，再做补单。")
            return
        quantity = self.resubmit_qty_spin.value()
        order_type = "MKT" if self.resubmit_mode_combo.currentText() == "市价" else "OPPONENT"
        tif, outside_rth = self.current_common_order_settings()
        if not self.confirm_order_submission(
            "确认补单",
            f"将对 {record.symbol} 重新下单：\n"
            f"方向 {record.action}\n"
            f"数量 {quantity:,}\n"
            f"模式 {self.resubmit_mode_combo.currentText()}\n\n"
            "这是第一次确认。是否继续？",
            f"最终确认将直接向 TWS 提交补单：\n"
            f"{record.symbol} {record.action} {quantity:,} {order_type}\n"
            f"账户 {self.current_account()} / TIF {tif}\n\n"
            "确认发单后将不能通过界面撤回本次提交。是否立即发送？",
        ):
            return
        settings = self.gather_connection_settings()
        self.start_worker(
            lambda: place_single_symbol_order(
                settings,
                symbol=record.symbol,
                action=record.action,
                quantity=quantity,
                order_type=order_type,
                tif=tif,
                outside_rth=outside_rth,
                limit_price=None,
            ),
            self.on_resubmit_monitor_order_finished,
            busy_message="提交补单中",
            log_message=f"开始补单 {record.symbol} {record.action} {quantity:,} {order_type}",
        )

    def on_resubmit_monitor_order_finished(self, payload) -> None:
        snapshot, order = payload
        self.snapshot = snapshot
        self.register_monitor_orders((order,), "补单")
        price_text = f" @ {order.limit_price:.4f}" if order.limit_price is not None else ""
        summary = (
            f"{order.symbol} {order.action} {order.quantity:,} {order.order_type}{price_text} "
            f"status={order.status} orderId={order.order_id} permId={order.perm_id}"
        )
        self.append_log("补单提交完成")
        self.append_log(summary)
        QMessageBox.information(self, "补单提交完成", summary)
        self.refresh_order_monitor_tab()

    def can_execute_component_buy(self) -> bool:
        return bool(self.snapshot and self.current_account() and self.planned_component_orders("BUY"))

    def can_execute_component_sell(self) -> bool:
        if not (self.snapshot and self.current_account() and self.component_rows() and self.planned_component_orders("SELL")):
            return False
        _, _, _, blockers = self.component_sell_summary()
        return not blockers

    def can_submit_xop(self) -> bool:
        if not self.snapshot or not self.current_account():
            return False
        if self.xop_qty_spin.value() <= 0:
            return False
        if self.xop_order_type_combo.currentText() == "LMT" and self.xop_price_spin.value() <= 0:
            return False
        return True

    def update_xop_price_input_state(self) -> None:
        is_limit = self.xop_order_type_combo.currentText() == "LMT"
        self.xop_price_spin.setEnabled(is_limit)
        if is_limit and self.xop_price_spin.value() <= 0:
            market_price = self.xop_market_price()
            if market_price > 0:
                self.xop_price_spin.setValue(round(market_price, 2))

    def update_execution_controls(self) -> None:
        active = self.active_workers == 0
        self.buy_components_button.setEnabled(active and self.can_execute_component_buy())
        self.sell_components_button.setEnabled(active and self.can_execute_component_sell())
        self.submit_xop_button.setEnabled(active and self.can_submit_xop())

        if not self.basket:
            self.component_note.setText("先在菜单“设置”里配置篮子文件。")
        elif not self.snapshot:
            self.component_note.setText("先连接 TWS，再按需分别刷新持仓和券源。")
        elif not self.current_account():
            self.component_note.setText("下单前需要明确选择 IBKR 账户。")
        else:
            component_count, ready_count, covered_qty, blockers = self.component_sell_summary()
            sell_orders = self.planned_component_orders("SELL")
            buy_orders = self.planned_component_orders("BUY")
            target_xop_shares = self.component_target_xop_spin.value()
            if not self.component_rows():
                self.component_note.setText(f"当前篮子没有除 {BASE_SYMBOL} 以外的成分股行。")
            elif not sell_orders and not buy_orders:
                self.component_note.setText(
                    f"当前成分股持仓已满足 {target_xop_shares:,} 股 {BASE_SYMBOL} 等价目标。"
                )
            elif blockers:
                parts = []
                for symbol, shortfall, pending_data in blockers[:4]:
                    if pending_data:
                        parts.append(f"{symbol} 待查券源")
                    else:
                        parts.append(f"{symbol} 缺 {fmt_qty(shortfall)}")
                self.component_note.setText(
                    f"目标 {target_xop_shares:,} 股 {BASE_SYMBOL} 等价空头；当前还需补卖 "
                    f"{sum(item.quantity for item in sell_orders):,} 股，"
                    f"{ready_count}/{component_count} 行可执行，覆盖 {covered_qty:,} 股；阻塞项：{'，'.join(parts)}"
                )
            else:
                self.component_note.setText(
                    f"目标 {target_xop_shares:,} 股 {BASE_SYMBOL} 等价空头；"
                    f"当前还需补卖 {sum(item.quantity for item in sell_orders):,} 股，"
                    f"如有超配行可买回 {sum(item.quantity for item in buy_orders):,} 股。"
                )

        market_price = self.xop_market_price()
        tif, outside_rth = self.current_common_order_settings()
        outside_text = "On" if outside_rth else "Off"
        price_hint = f"当前参考价 {market_price:.2f}" if market_price > 0 else "当前未拿到 XOP 行情"
        self.xop_note.setText(
            f"{price_hint}。通用参数 {tif} / Outside RTH {outside_text}；"
            f"{BASE_SYMBOL} 数量和限价均由你手动控制。"
        )

    def submit_component_basket(self, action: str) -> None:
        action = action.upper()
        rows = self.planned_component_orders(action)
        if not rows:
            message = "当前不需要再补卖成分股。" if action == "SELL" else "当前不需要买回成分股。"
            QMessageBox.information(self, "无需下单", message)
            return
        if action == "SELL" and not self.can_execute_component_sell():
            QMessageBox.warning(self, "无法卖出", "当前成分股篮子的现货/券源校验未通过。")
            return
        if action == "BUY" and not self.can_execute_component_buy():
            QMessageBox.warning(self, "无法买入", "请先连接 TWS、选择账户并导入篮子。")
            return

        pricing_mode = "MKT" if self.component_mode_combo.currentText() == "市价" else "OPPONENT"
        preview_rows = [f"{row.symbol}  x  {row.quantity:,}" for row in rows[:12]]
        if len(rows) > 12:
            preview_rows.append(f"... 其余 {len(rows) - 12} 行略")
        detail = "\n".join(preview_rows)
        action_text = "买回成分股到目标" if action == "BUY" else "卖出成分股到目标（含融券做空）"
        tif, outside_rth = self.current_common_order_settings()
        outside_text = "On" if outside_rth else "Off"
        if not self.confirm_order_submission(
            f"确认{action_text}",
            f"账户：{self.current_account()}\n"
            f"目标 {BASE_SYMBOL} 股数：{self.component_target_xop_spin.value():,}\n"
            f"模式：{self.component_mode_combo.currentText()}\n"
            f"TIF：{tif}\n"
            f"将提交 {len(rows)} 笔订单：\n\n{detail}\n\n"
            "这是第一次确认。是否继续？",
            f"最终确认将直接向 TWS 提交 {action_text}：\n"
            f"账户 {self.current_account()}\n"
            f"模式 {self.component_mode_combo.currentText()} / TIF {tif} / Outside RTH {outside_text}\n"
            f"订单笔数 {len(rows)}\n\n"
            "确认发单后将立即进入下单流程。是否现在发送？",
        ):
            return

        settings = self.gather_connection_settings()
        order_basket = BasketDocument(
            path=self.basket.path if self.basket else self.component_target_basket.path,
            name=self.component_target_basket.name if self.component_target_basket else "component_target",
            rows=rows,
            metadata={
                **(self.component_target_basket.metadata if self.component_target_basket else {}),
                "execution_action": action,
                "execution_mode": "delta_to_target",
            },
        )
        self.start_worker(
            lambda: place_component_basket_orders(
                settings,
                order_basket,
                action=action,
                pricing_mode=pricing_mode,
                tif=tif,
                outside_rth=outside_rth,
                base_symbol=BASE_SYMBOL,
            ),
            self.on_component_orders_finished,
            busy_message="提交成分股篮子中",
            log_message=f"开始提交成分股篮子 {action} 订单，共 {len(rows)} 笔",
        )

    def on_component_orders_finished(self, payload) -> None:
        snapshot, orders = payload
        self.snapshot = snapshot
        group_label = "成分股买入" if orders and orders[0].action == "BUY" else "成分股卖出"
        self.register_monitor_orders(orders, group_label)
        lines = []
        for order in orders:
            price_text = f" @ {order.limit_price:.2f}" if order.limit_price is not None else ""
            lines.append(
                f"{order.symbol} {order.action} {order.quantity:,} {order.order_type}{price_text} "
                f"status={order.status} orderId={order.order_id} permId={order.perm_id}"
            )
        summary = "\n".join(lines)
        self.append_log("成分股篮子订单提交完成")
        self.append_log(summary)
        QMessageBox.information(self, "提交完成", summary)
        self.refresh_holdings()

    def submit_xop_order(self) -> None:
        if not self.can_submit_xop():
            QMessageBox.warning(self, "无法下单", f"请先连接 TWS，并检查 {BASE_SYMBOL} 数量/价格输入。")
            return
        action = self.xop_side_combo.currentText()
        order_type = self.xop_order_type_combo.currentText()
        quantity = self.xop_qty_spin.value()
        limit_price = self.xop_price_spin.value() if order_type == "LMT" else None
        tif, outside_rth = self.current_common_order_settings()
        price_text = f" @ {limit_price:.4f}" if limit_price is not None else ""
        outside_text = "On" if outside_rth else "Off"
        if not self.confirm_order_submission(
            f"确认提交 {BASE_SYMBOL} 订单",
            f"账户：{self.current_account()}\n"
            f"{BASE_SYMBOL} {action} {quantity:,} {order_type}{price_text}\n"
            f"TIF：{tif}\n"
            "这是第一次确认。是否继续？",
            f"最终确认将直接向 TWS 提交 {BASE_SYMBOL} 订单：\n"
            f"{BASE_SYMBOL} {action} {quantity:,} {order_type}{price_text}\n"
            f"账户 {self.current_account()} / TIF {tif} / Outside RTH {outside_text}\n\n"
            "确认发单后将立即发送。是否现在提交？",
        ):
            return

        settings = self.gather_connection_settings()
        self.start_worker(
            lambda: place_single_symbol_order(
                settings,
                symbol=BASE_SYMBOL,
                action=action,
                quantity=quantity,
                order_type=order_type,
                tif=tif,
                outside_rth=outside_rth,
                limit_price=limit_price,
            ),
            self.on_xop_order_finished,
            busy_message=f"提交 {BASE_SYMBOL} 订单中",
            log_message=f"开始提交 {BASE_SYMBOL} {action} {quantity:,} {order_type}",
        )

    def on_xop_order_finished(self, payload) -> None:
        snapshot, order = payload
        self.snapshot = snapshot
        self.register_monitor_orders((order,), f"{BASE_SYMBOL} 手动")
        price_text = f" @ {order.limit_price:.4f}" if order.limit_price is not None else ""
        summary = (
            f"{order.symbol} {order.action} {order.quantity:,} {order.order_type}{price_text} "
            f"status={order.status} orderId={order.order_id} permId={order.perm_id}"
        )
        self.append_log(f"{BASE_SYMBOL} 订单提交完成")
        self.append_log(summary)
        QMessageBox.information(self, "提交完成", summary)
        self.refresh_holdings()


def build_application() -> QApplication:
    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    app.setFont(QFont("PingFang SC", 12))
    return app
