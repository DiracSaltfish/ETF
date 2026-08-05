from __future__ import annotations

import hashlib
import sys
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

import india_engine
from india_calendar import parse_holidays
from india_config import IndiaConfig, load_json_config, save_json_config
from india_models import RedemptionEvent
from india_order_planner import build_inda_close_plan, build_swap_plan, plan_display_rows, validate_preview_only
from india_sources import load_position_snapshots, load_qmt_accounts, load_redemption_statement
from india_store import IndiaStore


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"


def _parse_decimal(text: str) -> Decimal | None:
    value = text.strip().replace(",", "")
    if not value:
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        raise ValueError(f"金额/净值格式无效：{text}") from None


def _fmt(value: object) -> str:
    if value is None or value == "":
        return "--"
    if isinstance(value, Decimal):
        return f"{value:,.2f}"
    if isinstance(value, int):
        return f"{value:,}"
    return str(value)


def _table(headers: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(headers))
    table.setHorizontalHeaderLabels(headers)
    table.setEditTriggers(QTableWidget.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectRows)
    table.setAlternatingRowColors(True)
    table.horizontalHeader().setStretchLastSection(True)
    return table


def _fill(table: QTableWidget, rows: list[list[object]]) -> None:
    table.setRowCount(0)
    for row in rows:
        index = table.rowCount()
        table.insertRow(index)
        for column, value in enumerate(row):
            table.setItem(index, column, QTableWidgetItem(_fmt(value)))


class IndiaMainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("164824 印度基金赎回收益计算器")
        self.resize(1480, 900)
        self.raw_config = load_json_config(CONFIG_PATH)
        self.config = IndiaConfig.from_mapping(self.raw_config)
        self.store = IndiaStore(ROOT / "data" / "india_redemption.sqlite3")
        self.pending_statement_events: tuple[RedemptionEvent, ...] = ()
        self.calculation = None

        self.as_of_edit = QLineEdit(date.today().isoformat())
        self.fx_spin = QDoubleSpinBox()
        self.fx_spin.setRange(0.000001, 1000)
        self.fx_spin.setDecimals(6)
        self.fx_spin.setValue(float(self.raw_config.get("fx_rate") or 6.8))
        self.account_combo = QComboBox()
        self.account_combo.addItems(["QMT1", "QMT2", "QMT3"])
        self.manual_qty = QSpinBox()
        self.manual_qty.setRange(1, 100_000_000)
        self.manual_qty.setSingleStep(270_000)
        self.manual_qty.setValue(270_000)
        self.manual_net = QLineEdit()
        self.manual_nav = QLineEdit()
        self.manual_contract = QLineEdit()
        self.statement_path = QLineEdit(str(self.raw_config.get("redemption_statement_path") or ""))
        self.ib_path = QLineEdit(str(self.raw_config.get("ib_path") or ""))
        self.position_root = QLineEdit(str(self.raw_config.get("position_root") or "/Users/ellis/Desktop/交易表格"))
        self.path_edits: dict[str, QLineEdit] = {}
        for account in ("QMT1", "QMT2", "QMT3"):
            self.path_edits[account] = QLineEdit(str(self.raw_config.get(f"{account.lower()}_path") or ""))

        self.status_label = QLabel("等待读取数据")
        self.status_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.account_table = _table([
            "账户", "最新持仓", "三日最小持仓", "赎回预留", "最终可赎", "完整篮子", "零碎份额", "可信度", "最新快照"
        ])
        self.basket_table = _table([
            "篮子号", "账户", "赎回日", "T+5交割", "T+6可用", "份额", "国内FIFO成本", "净赎回款",
            "国内收益", "对冲收益RMB", "总收益RMB", "对冲状态", "结算状态", "提示"
        ])
        self.order_table = _table([
            "序号", "标的", "方向", "数量", "美东触发时间", "北京时间", "合约月", "用途", "OrderRef"
        ])
        self.warning_text = QTextEdit()
        self.warning_text.setReadOnly(True)
        self.warning_text.setMaximumHeight(115)

        central = QWidget()
        root_layout = QVBoxLayout(central)
        root_layout.addWidget(self._build_source_box())
        root_layout.addWidget(self._build_manual_box())
        root_layout.addWidget(self._build_order_box())
        root_layout.addWidget(self.order_table)
        root_layout.addWidget(QLabel("三账户当日可赎数量（买入后满 3 个中国基金交易日）"))
        root_layout.addWidget(self.account_table)
        root_layout.addWidget(QLabel("篮子汇总：270,000 份 = 1 个 NIFTY + 970 股 INDA；零碎份额不自动下对冲单"))
        root_layout.addWidget(self.basket_table, 1)
        root_layout.addWidget(QLabel("异常、规则闸门与数据来源"))
        root_layout.addWidget(self.warning_text)
        root_layout.addWidget(self.status_label)
        self.setCentralWidget(central)
        self.refresh_calculation()

    def _build_source_box(self) -> QGroupBox:
        box = QGroupBox("数据源")
        layout = QGridLayout(box)
        layout.addWidget(QLabel("计算日"), 0, 0)
        layout.addWidget(self.as_of_edit, 0, 1)
        layout.addWidget(QLabel("USD/CNH"), 0, 2)
        layout.addWidget(self.fx_spin, 0, 3)
        row = 1
        layout.addWidget(QLabel("持仓快照根目录"), row, 0)
        layout.addWidget(self.position_root, row, 1, 1, 3)
        position_button = QPushButton("浏览")
        position_button.clicked.connect(lambda: self._choose_directory(self.position_root, "选择持仓快照根目录"))
        layout.addWidget(position_button, row, 4)
        row += 1
        for account in ("QMT1", "QMT2", "QMT3"):
            layout.addWidget(QLabel(f"{account} 成交明细（成本，可选）"), row, 0)
            layout.addWidget(self.path_edits[account], row, 1, 1, 3)
            layout.addWidget(self._browse_button(self.path_edits[account], "选择QMT文件"), row, 4)
            row += 1
        layout.addWidget(QLabel("164824赎回交割单"), row, 0)
        layout.addWidget(self.statement_path, row, 1, 1, 3)
        layout.addWidget(self._browse_button(self.statement_path, "选择交割单"), row, 4)
        row += 1
        layout.addWidget(QLabel("IB Flex活动CSV"), row, 0)
        layout.addWidget(self.ib_path, row, 1, 1, 3)
        layout.addWidget(self._browse_button(self.ib_path, "选择IB CSV"), row, 4)
        row += 1
        buttons = QHBoxLayout()
        refresh = QPushButton("刷新可赎量 / 重算")
        refresh.clicked.connect(self.refresh_calculation)
        save = QPushButton("保存路径配置")
        save.clicked.connect(self.save_paths)
        import_statement = QPushButton("导入赎回交割单（预览后写入）")
        import_statement.clicked.connect(self.import_statement)
        buttons.addWidget(refresh)
        buttons.addWidget(save)
        buttons.addWidget(import_statement)
        buttons.addStretch(1)
        layout.addLayout(buttons, row, 0, 1, 5)
        return box

    def _build_manual_box(self) -> QGroupBox:
        box = QGroupBox("手工登记当日赎回")
        form = QFormLayout(box)
        form.addRow("账户", self.account_combo)
        form.addRow("赎回份额", self.manual_qty)
        form.addRow("合同号（可选）", self.manual_contract)
        form.addRow("实际净赎回款（可选）", self.manual_net)
        form.addRow("单位净值（可选，未填净款时估算）", self.manual_nav)
        button = QPushButton("登记并重算")
        button.clicked.connect(self.add_manual_redemption)
        form.addRow(button)
        return box

    def _build_order_box(self) -> QGroupBox:
        box = QGroupBox("印度对冲订单预览（只生成预览，不发送）")
        layout = QHBoxLayout(box)
        self.order_basket_spin = QSpinBox()
        self.order_basket_spin.setRange(1, 10_000)
        self.order_basket_spin.setValue(1)
        self.close_qty_spin = QSpinBox()
        self.close_qty_spin.setRange(970, 10_000_000)
        self.close_qty_spin.setValue(970)
        button = QPushButton("生成换仓 + INDA分段平仓计划")
        button.clicked.connect(self.generate_orders)
        layout.addWidget(QLabel("篮子数"))
        layout.addWidget(self.order_basket_spin)
        layout.addWidget(QLabel("实际INDA开仓股数"))
        layout.addWidget(self.close_qty_spin)
        layout.addWidget(button)
        layout.addStretch(1)
        return box

    @staticmethod
    def _browse_button(edit: QLineEdit, title: str) -> QPushButton:
        button = QPushButton("浏览")
        button.clicked.connect(lambda: IndiaMainWindow._choose_file(edit, title))
        return button

    @staticmethod
    def _choose_file(edit: QLineEdit, title: str) -> None:
        path, _filter = QFileDialog.getOpenFileName(None, title, str(Path.home()), "表格 (*.xlsx *.xls *.csv);;所有文件 (*)")
        if path:
            edit.setText(path)

    @staticmethod
    def _choose_directory(edit: QLineEdit, title: str) -> None:
        path = QFileDialog.getExistingDirectory(None, title, edit.text().strip() or str(Path.home()))
        if path:
            edit.setText(path)

    def _as_of_day(self) -> date:
        try:
            return date.fromisoformat(self.as_of_edit.text().strip())
        except ValueError:
            raise ValueError("计算日必须是 YYYY-MM-DD") from None

    def _current_config(self) -> IndiaConfig:
        values = dict(self.raw_config)
        values["fx_rate"] = f"{self.fx_spin.value():.6f}"
        return IndiaConfig.from_mapping(values)

    def _paths(self) -> dict[str, str | None]:
        return {account: edit.text().strip() or None for account, edit in self.path_edits.items()}

    def save_paths(self) -> None:
        self.raw_config.update({f"{account.lower()}_path": edit.text().strip() for account, edit in self.path_edits.items()})
        self.raw_config["position_root"] = self.position_root.text().strip()
        self.raw_config["redemption_statement_path"] = self.statement_path.text().strip()
        self.raw_config["ib_path"] = self.ib_path.text().strip()
        self.raw_config["fx_rate"] = f"{self.fx_spin.value():.6f}"
        save_json_config(CONFIG_PATH, self.raw_config)
        self.status_label.setText("路径配置已保存")

    def _all_events(self) -> list[RedemptionEvent]:
        events = self.store.list_redemptions()
        known = {item.event_id for item in events}
        events.extend(item for item in self.pending_statement_events if item.event_id not in known)
        return events

    def refresh_calculation(self) -> None:
        try:
            config = self._current_config()
            as_of_day = self._as_of_day()
            records = load_qmt_accounts(
                self._paths(),
                config.fund_code,
                self.raw_config.get("qmt_time_root") or self.raw_config.get("data_root") or None,
            )
            events = self._all_events()
            position_snapshots = load_position_snapshots(self.position_root.text().strip() or None, config.fund_code)
            self.calculation = india_engine.calculate(
                records,
                events,
                config,
                fx_rate=Decimal(str(self.fx_spin.value())),
                holidays=parse_holidays(self.raw_config.get("china_market_holidays")),
                fund_closed_days=parse_holidays(self.raw_config.get("fund_closed_days")),
                calendar_years=self.raw_config.get("china_calendar_years") or (),
                ib_fills=india_engine.load_ib_india_fills(self.ib_path.text().strip() or None),
                position_snapshots=position_snapshots,
                as_of_day=as_of_day,
            )
            self._fill_account_table()
            self._fill_basket_table()
            warnings = list(self.calculation.warnings)
            for item in self.calculation.baskets:
                warnings.extend(f"{item.basket_id}: {warning}" for warning in item.warnings)
            self.warning_text.setPlainText("\n".join(dict.fromkeys(warnings)) or "无")
            self.status_label.setText(
                f"已读取持仓快照 {len(position_snapshots)} 条、QMT成本明细 {len(records)} 条；"
                f"赎回事件 {len(self.calculation.redemptions)} 条；标准篮子 {self.calculation.standard_basket_count} 个；"
                f"总收益 {_fmt(self.calculation.total_pnl_cny)} 元"
            )
        except Exception as exc:
            self.calculation = None
            self.warning_text.setPlainText(str(exc))
            self.status_label.setText("计算失败")

    def _fill_account_table(self) -> None:
        rows = []
        for account in ("QMT1", "QMT2", "QMT3"):
            item = self.calculation.account_snapshots.get(account, {})
            rows.append([
                account,
                item.get("total_qty", 0),
                item.get("snapshot_eligible_qty", item.get("eligible_qty", 0)),
                item.get("reserved_qty", 0),
                item.get("eligible_qty", 0),
                item.get("full_baskets", int(item.get("eligible_qty", 0)) // self.config.basket_fund_qty),
                item.get("residual_qty", int(item.get("eligible_qty", 0)) % self.config.basket_fund_qty),
                item.get("confidence", "--"),
                item.get("last_trade_day") or "--",
            ])
        _fill(self.account_table, rows)

    def _fill_basket_table(self) -> None:
        rows = []
        for item in self.calculation.baskets:
            rows.append([
                item.basket_id,
                item.account,
                item.redeem_day,
                item.settlement.expected_statement_day if item.settlement else None,
                item.settlement.expected_available_day if item.settlement else None,
                item.redeem_qty,
                item.domestic_cost,
                item.domestic_net_amount,
                item.domestic_pnl,
                item.hedge.pnl_cny,
                item.total_pnl_cny,
                item.hedge_status,
                item.settlement_status,
                "；".join(item.warnings) or "--",
            ])
        _fill(self.basket_table, rows)

    def add_manual_redemption(self) -> None:
        try:
            # The calculation date and source paths are editable. Recompute
            # immediately before the limit check so a stale screen cannot be
            # used to authorize a redemption.
            self.refresh_calculation()
            day = self._as_of_day()
            net = _parse_decimal(self.manual_net.text())
            nav = _parse_decimal(self.manual_nav.text())
            account = self.account_combo.currentText()
            qty = self.manual_qty.value()
            contract = self.manual_contract.text().strip()
            event_id = hashlib.sha1(f"manual|{account}|{day}|{qty}|{contract}".encode("utf-8")).hexdigest()[:20]
            full_event_id = f"manual:{event_id}"
            existing = {item.event_id: item for item in self.store.list_redemptions()}.get(full_event_id)
            if self.calculation is None:
                raise ValueError("当前计算结果不可用，请先刷新持仓数据")
            available = int(self.calculation.account_snapshots.get(account, {}).get("eligible_qty", 0))
            allowed = available + (existing.qty if existing is not None else 0)
            if qty > allowed:
                raise ValueError(f"{account} 当前最多可登记 {allowed:,} 份，已阻止超额赎回 {qty:,} 份")
            event = RedemptionEvent(
                event_id=full_event_id,
                account=account,
                redeem_day=day,
                qty=qty,
                source="manual",
                contract_no=contract,
                net_amount=net,
                nav_per_share=nav,
            )
            self.store.upsert_redemption(event)
            self.refresh_calculation()
        except Exception as exc:
            QMessageBox.warning(self, "手工登记失败", str(exc))

    def import_statement(self) -> None:
        path = self.statement_path.text().strip()
        if not path:
            QMessageBox.warning(self, "缺少文件", "请先选择 164824 赎回交割单。")
            return
        try:
            imported = load_redemption_statement(path, self.config.fund_code)
            preview = "\n".join(
                f"{item.account} {item.redeem_day} {item.qty:,}份 净款={_fmt(item.net_amount)}"
                for item in imported.events[:20]
            ) or "未识别到赎回行"
            if imported.issues:
                preview += "\n\n异常：\n" + "\n".join(item.message for item in imported.issues[:10])
            answer = QMessageBox.question(
                self,
                "交割单导入预览",
                preview + "\n\n确认写入本地 SQLite 主账吗？",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            for event in imported.events:
                self.store.upsert_redemption(event)
            self.pending_statement_events = ()
            self.refresh_calculation()
        except Exception as exc:
            QMessageBox.warning(self, "交割单导入失败", str(exc))

    def generate_orders(self) -> None:
        try:
            day = self._as_of_day()
            config = self._current_config()
            basket_count = self.order_basket_spin.value()
            close_qty = self.close_qty_spin.value()
            specs = list(build_swap_plan(day, basket_count, config))
            specs.extend(build_inda_close_plan(day, close_qty, config))
            validate_preview_only(config, specs)
            _fill(self.order_table, plan_display_rows(specs))
            self.store.save_order_specs(specs)
            self.status_label.setText(f"已生成 {len(specs)} 条印度订单预览；没有发送到 TWS")
        except Exception as exc:
            QMessageBox.warning(self, "订单预览失败", str(exc))


def main() -> int:
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    window = IndiaMainWindow()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
