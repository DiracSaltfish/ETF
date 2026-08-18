from __future__ import annotations

import unittest
import tempfile
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication, QLabel, QMessageBox

import redemption_engine as engine
import basket_calibration
import market_data
import settlement_estimator
import szse_pcf
from redemption_ui import (
    ArrivalCalibrationTab,
    BasketMappingTab,
    DEFAULT_CONFIG,
    IbSelfCloseTab,
    MainWindow,
    OVERRIDES_PATH,
    PredictedRefundWorker,
    RealtimePremiumTab,
    SettingsDialog,
    SzsePcfTab,
    XopCloseOrdersTab,
    input_path_errors,
    normalize_business_day,
    pcf_field_reference_day_text,
    preferred_ui_font_family,
    shift_business_day,
)
import realtime_premium


SAMPLE_ROOT = Path("/Users/ellis/Desktop/ETF交割/6.22")


def write_cached_target_pcf(cache_root: Path, trading_day: date, cash_component: Decimal) -> None:
    path = cache_root / trading_day.isoformat() / "xml" / "159518.xml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<PCFFile>\n"
        "  <SecurityID>159518</SecurityID>\n"
        f"  <EstimateCashComponent>{cash_component}</EstimateCashComponent>\n"
        f"  <TradingDay>{trading_day:%Y%m%d}</TradingDay>\n"
        "</PCFFile>\n",
        encoding="utf-8",
    )


class BasketMappingTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.result = engine.calculate(
            {"QMT1": SAMPLE_ROOT / "qmt1.xlsx", "QMT2": None},
            SAMPLE_ROOT / "U15286908_20260601_20260629.csv",
            Decimal("6.79635"),
        )

    def setUp(self) -> None:
        self.tab = BasketMappingTab()
        self.tab.update_data(self.result)

    def tearDown(self) -> None:
        self.tab.close()

    def test_all_baskets_have_domestic_and_ib_links(self) -> None:
        self.assertEqual(self.tab.basket_table.rowCount(), 7)
        for basket in self.result.baskets:
            self.assertTrue(self.tab.domestic_basket_rows[basket.id])
            self.assertTrue(self.tab.ib_basket_rows[basket.id])

    def test_single_day_filter_keeps_only_that_redemption(self) -> None:
        selected = date(2026, 6, 22)
        value = self.tab._qdate(selected)
        self.tab.start_date.setDate(value)
        self.tab.end_date.setDate(value)
        self.tab.populate()
        self.assertEqual(self.tab.basket_table.rowCount(), 1)
        self.assertEqual(list(self.tab.basket_rows), [self.result.baskets[0].id])

    def test_ib_lane_uses_engine_self_pairs_and_unmatched_slices(self) -> None:
        rows = self.tab._ib_rows(date.min, date.max)
        self.assertEqual(
            sum(row["kind"] == "ib_self" for row in rows),
            len(self.result.ib_self_closes) * 2,
        )
        self.assertEqual(
            sum(row["kind"] == "unallocated" for row in rows),
            len(self.result.unmatched_ib),
        )


class IbSelfCloseTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_displays_self_close_pnl_and_unmatched_risk(self) -> None:
        opening = engine.IbSlice(
            trade_id="sell-1",
            dt=datetime(2026, 7, 9, 15, 40),
            side="SELL",
            qty=100,
            price=Decimal("12"),
            gross=Decimal("1200"),
            commission=Decimal("1"),
            role="direct_open",
        )
        closing = engine.IbSlice(
            trade_id="buy-1",
            dt=datetime(2026, 7, 10, 9, 30),
            side="BUY",
            qty=100,
            price=Decimal("10"),
            gross=Decimal("1000"),
            commission=Decimal("1"),
            role="direct_close",
        )
        unmatched = engine.IbSlice(
            trade_id="buy-2",
            dt=datetime(2026, 7, 10, 9, 31),
            side="BUY",
            qty=25,
            price=Decimal("9"),
            gross=Decimal("225"),
            commission=Decimal("0.25"),
            role="direct_close",
        )
        pair = engine.IbSelfClose(
            sequence=1,
            opening=opening,
            closing=closing,
            trade_pnl_usd=Decimal("198"),
            fx_rate=Decimal("7"),
        )
        result = engine.CalculationResult(
            baskets=(),
            venue_closes=(),
            account_transfers=(),
            qmt_records=(),
            ib_trades=(),
            borrow_fees=(),
            unallocated_ib_sell_qty=0,
            unallocated_ib_buy_qty=25,
            qmt_latest_day=date(2026, 7, 10),
            ib_self_closes=(pair,),
            unmatched_ib=(unmatched,),
            residual_ib_sell_qty=100,
            residual_ib_buy_qty=125,
        )
        tab = IbSelfCloseTab()

        tab.update_data(result)

        self.assertEqual(tab.self_table.rowCount(), 1)
        self.assertEqual(tab.unmatched_table.rowCount(), 1)
        self.assertEqual(tab.metric_values["matched"].text(), "1 组  /  100 股")
        self.assertEqual(tab.metric_values["cny"].text(), "1,386.00 RMB")
        self.assertIn("BUY 25", tab.metric_values["unmatched"].text())
        self.assertEqual(tab.self_table.item(0, 11).text(), "1,386.00")
        self.assertEqual(tab.self_table.item(0, 0).background().color().name(), "#ecfdf3")
        self.assertEqual(tab.unmatched_table.item(0, 0).background().color().name(), "#fffbeb")
        tab.close()


class MainWindowCalculationTest(unittest.TestCase):
    def test_shutdown_is_idempotent_and_never_waits_for_background_work(self) -> None:
        prediction_worker = Mock()
        prediction_thread = Mock()
        prediction_thread.isRunning.return_value = True
        refresh_timer = Mock()
        watcher = Mock()
        pcf_tab = Mock()
        realtime_tab = Mock()
        window = SimpleNamespace(
            _shutting_down=False,
            _prediction_worker=prediction_worker,
            _prediction_worker_thread=prediction_thread,
            refresh_timer=refresh_timer,
            watcher=watcher,
            pcf_tab=pcf_tab,
            realtime_premium_tab=realtime_tab,
        )

        MainWindow.shutdown(window)
        MainWindow.shutdown(window)

        self.assertTrue(window._shutting_down)
        refresh_timer.stop.assert_called_once_with()
        watcher.blockSignals.assert_called_once_with(True)
        prediction_worker.cancel.assert_called_once_with()
        prediction_thread.requestInterruption.assert_called_once_with()
        prediction_thread.quit.assert_called_once_with()
        prediction_thread.wait.assert_not_called()
        pcf_tab.shutdown.assert_called_once_with()
        realtime_tab.shutdown.assert_called_once_with()

    def test_main_calculation_does_not_apply_calibration_hedge_targets(self) -> None:
        class Label:
            def setText(self, _text: str) -> None:
                pass

        calculation_result = SimpleNamespace(
            baskets=[SimpleNamespace(id="QMT1:2026-06-29:1")],
            settled_baskets=[],
            settled_total_cny=Decimal("0"),
            warnings=[],
        )
        window = SimpleNamespace(
            refreshing=False,
            selected_basket_id=lambda: None,
            status_label=Label(),
            input_paths=lambda: ({"QMT1": "qmt1.xlsx", "QMT2": None}, "ib.csv"),
            fx_spin=SimpleNamespace(value=lambda: 6.8),
            overrides={},
            market_holidays=lambda: (),
            config={"transfer_contract_gap": engine.DEFAULT_TRANSFER_CONTRACT_GAP},
            result=None,
            basket_by_id={},
            populate_all=lambda _selected_id: None,
            restart_watcher=lambda: None,
        )

        with (
            patch("redemption_ui.input_path_errors", return_value=[]),
            patch.object(engine, "calculate", return_value=calculation_result) as calculate,
        ):
            MainWindow.calculate(window)

        calculate.assert_called_once_with(
            {"QMT1": "qmt1.xlsx", "QMT2": None},
            "ib.csv",
            Decimal("6.8"),
            {},
            (),
            engine.DEFAULT_TRANSFER_CONTRACT_GAP,
            qmt_time_root="",
        )

    def test_empty_paths_do_not_open_blocking_error_dialog(self) -> None:
        class Label:
            text = ""

            def setText(self, text: str) -> None:
                self.text = text

        status = Label()
        window = SimpleNamespace(
            refreshing=False,
            selected_basket_id=lambda: None,
            status_label=status,
            input_paths=lambda: ({"QMT1": "", "QMT2": None}, ""),
            result=object(),
            basket_by_id={"old": object()},
            restart_watcher=lambda: None,
        )
        with (
            patch.object(engine, "calculate") as calculate,
            patch("redemption_ui.QMessageBox.critical") as critical,
        ):
            MainWindow.calculate(window)

        calculate.assert_not_called()
        critical.assert_not_called()
        self.assertFalse(window.refreshing)
        self.assertIsNone(window.result)
        self.assertEqual(window.basket_by_id, {})
        self.assertIn("QMT1未配置", status.text)
        self.assertIn("IB未配置", status.text)
        self.assertIn("数据源", status.text)

    def test_missing_files_are_reported_as_configuration_errors(self) -> None:
        errors = input_path_errors(
            {"QMT1": "/not-found/qmt1.xlsx", "QMT2": "/not-found/qmt2.xlsx"},
            "/not-found/ib.csv",
        )
        self.assertEqual(len(errors), 3)
        self.assertTrue(all("文件不存在" in item for item in errors))

    def test_manual_virtual_close_toggle_persists_override(self) -> None:
        calculate_calls = []
        window = SimpleNamespace(
            overrides={
                "basket-1": {
                    "open_trade_ids": ["sell-1"],
                    "close_trade_ids": ["buy-1"],
                }
            },
            calculate=lambda: calculate_calls.append("calculate"),
        )

        with patch.object(engine, "save_overrides") as save_overrides:
            MainWindow.set_manual_virtual_close(window, "basket-1", True)

        self.assertEqual(window.overrides["basket-1"]["open_trade_ids"], ["sell-1"])
        self.assertTrue(window.overrides["basket-1"]["manual_virtual_close"])
        self.assertNotIn("close_trade_ids", window.overrides["basket-1"])
        save_overrides.assert_called_once_with(OVERRIDES_PATH, window.overrides)
        self.assertEqual(calculate_calls, ["calculate"])

    def test_manual_refund_amount_persists_and_clears_override(self) -> None:
        calculate_calls = []
        window = SimpleNamespace(
            overrides={
                "basket-1": {
                    "open_trade_ids": ["sell-1"],
                    "manual_virtual_close": True,
                }
            },
            calculate=lambda: calculate_calls.append("calculate"),
        )

        with patch.object(engine, "save_overrides") as save_overrides:
            MainWindow.set_basket_manual_overrides(
                window,
                "basket-1",
                True,
                Decimal("1049930.126"),
            )

        self.assertEqual(window.overrides["basket-1"]["manual_refund_amount"], "1049930.13")
        self.assertTrue(window.overrides["basket-1"]["manual_virtual_close"])
        save_overrides.assert_called_once_with(OVERRIDES_PATH, window.overrides)
        self.assertEqual(calculate_calls, ["calculate"])

        with patch.object(engine, "save_overrides") as save_overrides:
            MainWindow.set_basket_manual_overrides(window, "basket-1", False, None)

        self.assertEqual(window.overrides["basket-1"], {"open_trade_ids": ["sell-1"]})
        save_overrides.assert_called_once_with(OVERRIDES_PATH, window.overrides)
        self.assertEqual(calculate_calls, ["calculate", "calculate"])


class BasketSummaryPresentationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_summary_hides_source_and_share_columns_and_marks_manual_refund_cell(self) -> None:
        config = dict(DEFAULT_CONFIG)
        config.update(
            {
                "qmt1_path": "",
                "qmt2_path": "",
                "ib_path": "",
                "shared_folder_path": "",
                "fx_rates_csv_path": "",
                "xop_price_csv_path": "",
                "calibration_csv_path": "",
                "settlement_observation_csv_path": "",
                "predicted_refund_csv_path": "",
            }
        )
        with patch("redemption_ui.load_config", return_value=config):
            window = MainWindow()
        basket = engine.BasketResult(
            id="basket-1",
            sequence=1,
            source="QMT2",
            redeem_day=date(2026, 7, 3),
            contract_no=123,
            redeem_qty=1_000_000,
            domestic_cost=Decimal("1030000"),
            refund_amount=Decimal("1040000"),
            manual_refund_amount=Decimal("1040000"),
            manual_refund_applied=True,
            cash_difference=Decimal("1600"),
            domestic_pnl=Decimal("11600"),
            hedge_target=990,
            ib_pnl_usd=Decimal("0"),
            ib_pnl_cny=Decimal("0"),
            total_pnl_cny=Decimal("11600"),
            status="待现金差额",
        )
        window.result = engine.CalculationResult(
            baskets=(basket,),
            venue_closes=(),
            account_transfers=(),
            qmt_records=(),
            ib_trades=(),
            borrow_fees=(),
            unallocated_ib_sell_qty=0,
            unallocated_ib_buy_qty=0,
            qmt_latest_day=date(2026, 7, 3),
        )
        window.predicted_refunds = {
            "basket-1": settlement_estimator.PredictedRefund(
                basket_id="basket-1",
                calculated_at="2026-07-04T10:00:00",
                redeem_day=date(2026, 7, 3),
                contract_no=123,
                redeem_qty=1_000_000,
                creation_redemption_unit=1_000_000,
                unit_ratio=Decimal("1"),
                shares_per_cu=Decimal("997"),
                estimated_xop_shares=Decimal("997"),
                price_window="1554_1557",
                xop_price=Decimal("154"),
                settlement_fx=Decimal("6.8"),
                predicted_refund_cny=Decimal("1044058.40"),
                predicted_cash_difference_cny=Decimal("1500"),
                predicted_basket_asset_cny=Decimal("1045558.40"),
                model_version=settlement_estimator.PREDICTED_BASKET_MODEL_VERSION,
            )
        }

        window.populate_baskets(None)
        headers = [
            window.basket_table.horizontalHeaderItem(column).text()
            for column in range(window.basket_table.columnCount())
        ]
        self.assertNotIn("来源", headers)
        self.assertNotIn("份额", headers)
        self.assertEqual(headers[headers.index("现金差额") + 1], "退款")
        self.assertEqual(headers[headers.index("退款") + 1], "篮子资产")
        refund_item = window.basket_table.item(0, headers.index("退款"))
        self.assertEqual(refund_item.text(), "1,040,000.00")
        self.assertEqual(refund_item.background().color().name(), "#f3e8ff")
        self.assertEqual(window.basket_table.item(0, headers.index("篮子资产")).text(), "1,041,600.00")
        self.assertEqual(window.basket_table.item(0, headers.index("预估篮子资产")).text(), "1,045,558.40")
        self.assertEqual(window.basket_table.item(0, headers.index("预估偏差")).text(), "--")
        self.assertEqual(window.basket_table.item(0, headers.index("赎回日")).text(), "07-03")

        window.detail_button.setChecked(True)
        detailed_headers = [
            window.basket_table.horizontalHeaderItem(column).text()
            for column in range(window.basket_table.columnCount())
        ]
        self.assertIn("来源", detailed_headers)
        self.assertNotIn("份额", detailed_headers)
        window.close()

    def test_prediction_worker_fetches_xop_window_and_persists_result(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xop_path = root / "xop.csv"
            fx_path = root / "fx.csv"
            pcf_cache_path = root / "pcf"
            predicted_path = root / "predicted.csv"
            fx_path.write_text(
                "trade_date,source,pair,display_name,quote_time,rate,raw_rate,quote_basis,raw_basis,fetched_at,derived_from\n"
                "2026-07-03,CFETS_REFERENCE_RATE,USD/CNY,USD/CNY,16:00,6.8,6.8,pair_standard,pair_standard,2026-07-03T16:01:00,\n",
                encoding="utf-8",
            )
            write_cached_target_pcf(pcf_cache_path, date(2026, 7, 3), Decimal("1500"))
            basket = engine.BasketResult(
                id="basket-1",
                sequence=1,
                source="QMT1",
                redeem_day=date(2026, 7, 3),
                contract_no=123,
                redeem_qty=1_000_000,
            )
            worker = PredictedRefundWorker(
                (basket,),
                xop_path,
                fx_path,
                pcf_cache_path,
                predicted_path,
                "127.0.0.1",
                7496,
                9888,
            )
            payloads = []
            worker.finished.connect(lambda payload: payloads.append(payload))
            with patch(
                "redemption_ui.backfill_xop_from_tws.fetch_prices",
                return_value=[
                    market_data.XopDailyPrice(
                        "XOP",
                        date(2026, 7, 3),
                        Decimal("154.1"),
                        last_1559=Decimal("154"),
                        source="tws_historical",
                    )
                ],
            ) as fetch_prices:
                worker.run()

            fetch_prices.assert_called_once()
            self.assertEqual(fetch_prices.call_args.kwargs["intraday_days"], {date(2026, 7, 3)})
            self.assertEqual(payloads[0]["errors"], [])
            saved = settlement_estimator.PredictedRefundStore(predicted_path).by_basket_id()["basket-1"]
            self.assertEqual(saved.predicted_refund_cny, Decimal("1043011.20"))
            self.assertEqual(saved.predicted_cash_difference_cny, Decimal("1500.00"))
            self.assertEqual(saved.predicted_basket_asset_cny, Decimal("1044511.20"))


class PortableStartupTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_window_opens_and_settings_remain_available_with_empty_paths(self) -> None:
        config = dict(DEFAULT_CONFIG)
        config.update(
            {
                "qmt1_path": "",
                "qmt2_path": "",
                "ib_path": "",
                "shared_folder_path": "",
                "fx_rates_csv_path": "",
                "xop_price_csv_path": "",
                "calibration_csv_path": "",
                "settlement_observation_csv_path": "",
            }
        )
        with (
            patch("redemption_ui.load_config", return_value=config),
            patch("redemption_ui.QMessageBox.critical") as critical,
        ):
            window = MainWindow()
            self.app.processEvents()

        critical.assert_not_called()
        self.assertTrue(window.settings_button.isEnabled())
        self.assertIn("数据源尚未就绪", window.status_label.text())
        dialog = SettingsDialog(window.config, window)
        self.assertEqual(dialog.qmt1.value(), "")
        self.assertEqual(dialog.qmt3.value(), "")
        self.assertEqual(dialog.ib.value(), "")
        dialog.close()
        window.close()


class RealtimePremiumTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_page_calculates_after_three_quotes_without_starting_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "pcf"
            write_cached_target_pcf(cache_path, date(2026, 7, 3), Decimal("1500"))
            tab = RealtimePremiumTab(
                {
                    "tws_host": "127.0.0.1",
                    "tws_port": 7496,
                    "tws_client_id": 8888,
                    "fx_rates_csv_path": str(Path(temp_dir) / "fx.csv"),
                    "szse_pcf_cache_dir": str(cache_path),
                }
            )
            now = datetime(2026, 7, 3, 15, 0)
            tab.update_xop_quote(
                realtime_premium.XopQuote(Decimal("150"), Decimal("150.1"), Decimal("150.05"), now)
            )
            tab.update_domestic_quote(
                realtime_premium.SinaQuote(
                    "159518", "标普油气", Decimal("1.050"), Decimal("1.051"),
                    100_000, 80_000, Decimal("1.050"), now, now,
                    bids=tuple(
                        realtime_premium.QuoteLevel(Decimal("1.050") - Decimal(index) / 1000, 100_000)
                        for index in range(5)
                    ),
                    asks=tuple(
                        realtime_premium.QuoteLevel(Decimal("1.051") + Decimal(index) / 1000, 80_000)
                        for index in range(5)
                    ),
                )
            )
            tab.update_cfets_quote(
                realtime_premium.CfetsQuote(Decimal("7"), date(2026, 7, 3), "15:00", "now")
            )
            self.assertEqual(tab.valuation_table.rowCount(), 8)
            self.assertEqual(tab.valuation_table.item(0, 1).text(), "996 股")
            self.assertEqual(tab.valuation_table.item(2, 1).text(), "1,500.00")
            self.assertEqual(tab.valuation_table.item(4, 1).text(), "1.047300")
            self.assertIn("+0.2578%", tab.valuation_table.item(6, 1).text())
            self.assertEqual(tab.order_book_table.rowCount(), 10)
            self.assertEqual(tab.order_book_table.item(0, 0).text(), "卖5")
            self.assertEqual(tab.order_book_table.item(4, 0).text(), "卖1")
            self.assertEqual(tab.order_book_table.item(5, 0).text(), "买1")
            self.assertEqual(tab.order_book_table.item(9, 0).text(), "买5")
            self.assertFalse(tab.started)
            tab.shutdown()
            tab.close()

    def test_automatic_connections_only_run_in_domestic_session(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tab = RealtimePremiumTab({"fx_rates_csv_path": str(Path(temp_dir) / "fx.csv")})
            with patch("redemption_ui.beijing_now", return_value=datetime(2026, 7, 3, 8, 30)), \
                 patch.object(realtime_premium, "is_auto_connection_window", return_value=False), \
                 patch.object(tab.tws_client, "connect_tws") as connect_ib, \
                 patch.object(tab, "start_sina") as start_sina:
                tab._automatic_connection_tick()
            connect_ib.assert_not_called()
            start_sina.assert_not_called()

            with patch("redemption_ui.beijing_now", return_value=datetime(2026, 7, 3, 10, 0)), \
                 patch.object(realtime_premium, "is_auto_connection_window", return_value=True), \
                 patch.object(tab.tws_client, "connect_tws") as connect_ib, \
                 patch.object(tab, "start_sina") as start_sina:
                tab._automatic_connection_tick()
            connect_ib.assert_called_once_with()
            start_sina.assert_called_once_with(manual=False)
            tab.shutdown()
            tab.close()

    def test_connection_status_uses_prominent_state_colors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            tab = RealtimePremiumTab({"fx_rates_csv_path": str(Path(temp_dir) / "fx.csv")})
            tab.update_ib_status("IB已连接并订阅XOP实时Bid/Ask", True)
            self.assertIn("#dcfce7", tab.ib_status.styleSheet())
            tab.update_sina_status("新浪行情失败：连接中断", False)
            self.assertIn("#fee2e2", tab.sina_status.styleSheet())
            tab.shutdown()
            tab.close()


class SzsePcfTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_focus_list_displays_target_first_with_site_name(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            tab.current_index = tab.store.build_focus_day_index(date(2026, 7, 9))
            tab.populate_index("159518")

            self.assertEqual(tab.list_table.item(0, 0).text(), "159518")
            self.assertEqual(tab.list_table.item(0, 1).text(), "标普油气ETF嘉实")
            self.assertEqual(tab.selected_code(), "159518")
            self.assertEqual(tab.selected_key(), "SZ159518")
            self.assertEqual(tab.list_table.rowCount(), len(szse_pcf.FOCUS_FUND_KEYS))
            tab.close()

    def test_shutdown_cancels_workers_without_blocking_ui_thread(self) -> None:
        load_worker = Mock()
        prefetch_worker = Mock()
        load_thread = Mock()
        prefetch_thread = Mock()
        load_thread.isRunning.return_value = True
        prefetch_thread.isRunning.return_value = True
        tab = SimpleNamespace(
            _pending_request=(date(2026, 7, 14), False, "SZ159518"),
            _worker=load_worker,
            _prefetch_worker=prefetch_worker,
            _worker_thread=load_thread,
            _prefetch_thread=prefetch_thread,
        )

        SzsePcfTab.shutdown(tab)

        self.assertIsNone(tab._pending_request)
        load_worker.cancel.assert_called_once_with()
        prefetch_worker.cancel.assert_called_once_with()
        for thread in (load_thread, prefetch_thread):
            thread.requestInterruption.assert_called_once_with()
            thread.quit.assert_called_once_with()
            thread.wait.assert_not_called()

    def test_actual_cash_component_uses_later_cached_pcf_with_matching_pre_trading_day(self) -> None:
        def write_pcf(root: Path, cache_day: date, trading_day: date, pre_trading_day: date, cash: str) -> None:
            path = root / cache_day.isoformat() / "xml" / "159518.xml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "<PCFFile>\n"
                "<SecurityID>159518</SecurityID>\n"
                f"<TradingDay>{trading_day:%Y%m%d}</TradingDay>\n"
                f"<PreTradingDay>{pre_trading_day:%Y%m%d}</PreTradingDay>\n"
                "<EstimateCashComponent>1401.74</EstimateCashComponent>\n"
                f"<CashComponent>{cash}</CashComponent>\n"
                "</PCFFile>\n",
                encoding="utf-8",
            )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            target_day = date(2026, 6, 29)
            write_pcf(root, target_day, target_day, date(2026, 6, 25), "1401.74")
            tab = SzsePcfTab(root, root / "fx.csv")
            detail = tab.store.ensure_target_detail(target_day)
            self.assertIsNone(tab.actual_cash_component_from_cached_future_pcf(detail))
            write_pcf(root, date(2026, 7, 1), date(2026, 7, 1), target_day, "1638.17")

            self.assertEqual(
                tab.actual_cash_component_from_cached_future_pcf(detail),
                (date(2026, 7, 1), "1638.17"),
            )
            tab.populate_detail(detail)
            labels = [
                tab.summary_grid.itemAt(index).widget().findChild(QLabel).text()
                for index in range(tab.summary_grid.count())
            ]
            self.assertIn("当日实际现金差额 · 2026-06-29", labels)
            tab.close()

    def test_cached_pcf_rows_are_green_and_missing_rows_are_gray(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            trade_day = date(2026, 7, 9)
            cached_path = tab.store.day_dir(trade_day) / "xml" / "159518.xml"
            cached_path.parent.mkdir(parents=True)
            cached_path.write_text("<PCFFile />", encoding="utf-8")
            tab.current_index = tab.store.build_focus_day_index(trade_day)
            tab.populate_index("159518")

            self.assertEqual(tab.list_table.item(0, 0).foreground().color().name(), "#15803d")
            self.assertEqual(tab.list_table.item(1, 0).foreground().color().name(), "#64748b")
            tab.close()

    def test_startup_prefetch_only_requests_missing_cache_after_0815(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            expected_day = date(2026, 7, 3)
            with patch.object(tab, "_start_prefetch") as start_prefetch:
                tab.startup_prefetch_if_needed(datetime(2026, 7, 3, 8, 16))
            start_prefetch.assert_called_once()
            self.assertEqual(start_prefetch.call_args.args[0], expected_day)
            self.assertEqual(len(start_prefetch.call_args.args[1]), len(szse_pcf.FOCUS_FUND_KEYS))
            tab.close()

            cached_tab = SzsePcfTab(root / "cached-pcf", root / "fx.csv")
            for item in cached_tab.store.build_focus_day_index(expected_day).items:
                xml_path = cached_tab.store.day_dir(expected_day) / item.cache_xml_path
                xml_path.parent.mkdir(parents=True, exist_ok=True)
                if item.exchange == szse_pcf.EXCHANGE_SSE:
                    xml_path.write_text(
                        "<SSEPortfolioCompositionFile>"
                        f"<FundInstrumentID>{item.fund_code}</FundInstrumentID>"
                        f"<TradingDay>{expected_day:%Y%m%d}</TradingDay>"
                        "<ComponentList /></SSEPortfolioCompositionFile>",
                        encoding="utf-8",
                    )
                else:
                    xml_path.write_text("<PCFFile />", encoding="utf-8")
            with patch.object(cached_tab, "_start_prefetch") as start_prefetch:
                cached_tab.startup_prefetch_if_needed(datetime(2026, 7, 3, 8, 16))
            start_prefetch.assert_not_called()
            self.assertIn("已在本地缓存", cached_tab.status_label.text())
            cached_tab.close()

    def test_search_selects_focus_code_without_broad_fetch(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            tab.current_index = tab.store.build_focus_day_index(date(2026, 7, 9))
            tab.populate_index("159518")
            tab.search_edit.setText("159605")

            with patch.object(tab, "load_day") as load_day:
                tab.search_fund_code()

            self.assertEqual(tab.selected_code(), "159605")
            self.assertEqual(tab.selected_key(), "SZ159605")
            load_day.assert_called_once_with(selected_code="SZ159605")
            tab.close()

    def test_search_selects_sse_focus_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            tab.current_index = tab.store.build_focus_day_index(date(2026, 7, 9))
            tab.populate_index("SZ159518")
            tab.search_edit.setText("513100")

            with patch.object(tab, "load_day") as load_day:
                tab.search_fund_code()

            self.assertEqual(tab.selected_code(), "513100")
            self.assertEqual(tab.selected_key(), "SH513100")
            load_day.assert_called_once_with(selected_code="SH513100")
            tab.close()

    def test_date_change_keeps_current_selected_fund_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            tab.current_index = tab.store.build_focus_day_index(date(2026, 7, 9))
            tab.populate_index("159605")
            tab._loaded_once = True

            with patch.object(tab, "load_day") as load_day:
                tab.handle_date_changed(tab.date_edit.date())

            load_day.assert_called_once_with(selected_code="SZ159605")
            tab.close()

    def test_search_ignores_non_focus_codes_without_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            tab.search_edit.setText("160140")

            with patch.object(tab, "load_day") as load_day:
                tab.search_fund_code()

            load_day.assert_not_called()
            self.assertIn("未发起交易所请求", tab.status_label.text())
            tab.close()


class XopCloseOrdersTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def test_preview_is_unselected_and_each_selected_order_requires_confirmation(self) -> None:
        client = realtime_premium.TwsXopMarketData("127.0.0.1", 7496, 8888)
        with patch.object(client, "is_connected", return_value=True):
            tab = XopCloseOrdersTab(client)
            tab.trade_date_edit.setText("20300107")
            tab.total_qty_spin.setValue(990)
            tab.generate_preview()
            self.assertEqual(tab.order_table.rowCount(), 5)
            self.assertTrue(
                all(tab.order_table.item(row, 0).checkState() == Qt.Unchecked for row in range(5))
            )
            tab.order_table.item(0, 0).setCheckState(Qt.Checked)
            tab.order_table.item(1, 0).setCheckState(Qt.Checked)
            tab.unlock_checkbox.setChecked(True)
            self.assertTrue(tab.send_button.isEnabled())
            with (
                patch("redemption_ui.QMessageBox.question", side_effect=[QMessageBox.Yes, QMessageBox.No]) as question,
                patch.object(client, "submit_confirmed_order", return_value=True) as submit,
            ):
                tab.send_selected_orders()
            self.assertEqual(question.call_count, 2)
            self.assertTrue(all(call.args[-1] == QMessageBox.No for call in question.call_args_list))
            submit.assert_called_once()
            submitted_spec = submit.call_args.args[0]
            self.assertEqual(submitted_spec.sequence, 1)
            self.assertEqual(submitted_spec.quantity, 200)
            self.assertFalse(tab.unlock_checkbox.isChecked())
            self.assertIn("等待TWS回报", tab.order_table.item(0, tab.STATUS_COLUMN).text())
            self.assertIn("用户取消", tab.order_table.item(1, tab.STATUS_COLUMN).text())
            tab.close()
        client.disconnect_tws()

    def test_two_basket_preview_multiplies_manual_target_quantity(self) -> None:
        client = realtime_premium.TwsXopMarketData("127.0.0.1", 7496, 8888)
        tab = XopCloseOrdersTab(client)
        tab.trade_date_edit.setText("20300107")
        tab.total_qty_spin.setValue(990)
        tab.basket_count_combo.setCurrentIndex(tab.basket_count_combo.findData(2))
        tab.generate_preview()
        self.assertEqual(tab.order_table.rowCount(), 10)
        self.assertEqual([tab.order_table.item(row, 2).text() for row in range(10)], ["200"] * 9 + ["180"])
        self.assertEqual(tab.order_table.item(0, 3).text(), "20300107 15:58:45 US/Eastern")
        self.assertEqual(tab.order_table.item(9, 3).text(), "20300107 15:59:15 US/Eastern")
        self.assertIn("平仓 2 张", tab.event_log.toPlainText())
        self.assertIn("合计 1,980 股", tab.event_log.toPlainText())
        tab.close()
        client.disconnect_tws()

    def test_order_event_updates_log_without_crashing(self) -> None:
        client = realtime_premium.TwsXopMarketData("127.0.0.1", 7496, 8888)
        tab = XopCloseOrdersTab(client)
        tab.trade_date_edit.setText("20300107")
        tab.generate_preview()
        order_ref = tab.order_table.item(0, 13).text()
        tab.handle_order_event(
            {
                "event": "orderStatus",
                "order_ref": order_ref,
                "order_id": 12345,
                "status": "PreSubmitted",
                "message": "",
            }
        )
        self.assertIn("orderStatus", tab.event_log.toPlainText())
        self.assertIn("PreSubmitted", tab.event_log.toPlainText())
        self.assertEqual(tab.order_table.item(0, tab.ORDER_ID_COLUMN).text(), "12345")
        self.assertIn("PreSubmitted", tab.order_table.item(0, tab.STATUS_COLUMN).text())
        tab.close()
        client.disconnect_tws()


class BusinessDayHelperTest(unittest.TestCase):
    def test_normalize_business_day_moves_weekend_back_to_friday(self) -> None:
        self.assertEqual(normalize_business_day(date(2026, 7, 4)), date(2026, 7, 3))
        self.assertEqual(normalize_business_day(date(2026, 7, 5)), date(2026, 7, 3))

    def test_shift_business_day_skips_weekends(self) -> None:
        self.assertEqual(shift_business_day(date(2026, 7, 3), 1), date(2026, 7, 6))
        self.assertEqual(shift_business_day(date(2026, 7, 6), -1), date(2026, 7, 3))

    def test_pcf_field_reference_day_uses_pre_trading_day_for_nav_fields(self) -> None:
        metadata = {
            "TradingDay": "20260622",
            "PreTradingDay": "20260617",
        }
        self.assertEqual(pcf_field_reference_day_text(metadata, "CashComponent", date(2026, 6, 22)), "2026-06-17")
        self.assertEqual(pcf_field_reference_day_text(metadata, "NAVperCU", date(2026, 6, 22)), "2026-06-17")
        self.assertEqual(pcf_field_reference_day_text(metadata, "NAV", date(2026, 6, 22)), "2026-06-17")
        self.assertEqual(pcf_field_reference_day_text(metadata, "RedemptionLimit", date(2026, 6, 22)), "2026-06-22")


class FontCompatibilityTest(unittest.TestCase):
    def test_windows_prefers_microsoft_yahei_ui(self) -> None:
        family = preferred_ui_font_family(
            "win32",
            {"SimSun", "Segoe UI", "Microsoft YaHei", "Microsoft YaHei UI"},
        )
        self.assertEqual(family, "Microsoft YaHei UI")

    def test_windows_falls_back_without_using_simsun(self) -> None:
        family = preferred_ui_font_family("win32", {"SimSun", "Segoe UI"})
        self.assertEqual(family, "Segoe UI")

    def test_macos_keeps_pingfang(self) -> None:
        self.assertEqual(preferred_ui_font_family("darwin", {"PingFang SC"}), "PingFang SC")


class ArrivalCalibrationTabTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])
        cls.result = engine.calculate(
            {"QMT1": SAMPLE_ROOT / "qmt1.xlsx", "QMT2": None},
            SAMPLE_ROOT / "U15286908_20260601_20260629.csv",
            Decimal("6.79635"),
        )

    def test_empty_local_stores_show_all_baskets_without_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = {
                "fx_rates_csv_path": str(root / "fx.csv"),
                "xop_price_csv_path": str(root / "xop.csv"),
                "calibration_csv_path": str(root / "points.csv"),
                "settlement_observation_csv_path": str(root / "observations.csv"),
                "estimate_price_window": "1540_1600",
            }
            pcf_tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            tab = ArrivalCalibrationTab(config, pcf_tab)
            tab.update_data(self.result)
            self.assertEqual(tab.estimate_table.rowCount(), len(self.result.baskets))
            self.assertIn("PCF", tab.estimate_table.item(0, 13).text())
            self.assertEqual(tab.functional_tabs.count(), 4)
            self.assertEqual(tab.functional_tabs.tabText(0), "指定日期到账预估")
            self.assertEqual(tab.functional_tabs.tabText(3), "实际到账反校准")
            tab.close()
            pcf_tab.close()

    def test_selected_date_estimate_works_without_qmt_redemption(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            xop_path = root / "xop.csv"
            xop_path.write_text(
                "symbol,trade_day,close,vwap_1540_1550,vwap_1540_1600,last_1600,source\n"
                "XOP,2026-07-01,158,,,,manual\n"
                "XOP,2026-07-03,158,,158,,manual\n",
                encoding="utf-8",
            )
            fx_path = root / "fx.csv"
            fx_path.write_text(
                "trade_date,source,pair,display_name,quote_time,rate,raw_rate,quote_basis,raw_basis,fetched_at,derived_from\n"
                "2026-07-03,CFETS_REFERENCE_RATE,USD/CNY,USD/CNY,CLOSE,6.78,6.78,pair_standard,pair_standard,2026-07-03T18:00:00,18:00\n",
                encoding="utf-8",
            )
            config = {
                "fx_rates_csv_path": str(fx_path),
                "xop_price_csv_path": str(xop_path),
                "calibration_csv_path": str(root / "points.csv"),
                "settlement_observation_csv_path": str(root / "observations.csv"),
                "estimate_price_window": "1540_1600",
                "tws_host": "127.0.0.1",
                "tws_port": 7496,
                "tws_client_id": 8888,
            }
            pcf_tab = SzsePcfTab(root / "pcf", fx_path)
            tab = ArrivalCalibrationTab(config, pcf_tab)
            point = basket_calibration.PcfCalibrationPoint(
                pcf_trading_day=date(2026, 7, 3),
                valuation_day=date(2026, 7, 1),
                creation_redemption_unit=1_000_000,
                nav_per_cu=Decimal("0"),
                cash_component=Decimal("0"),
                estimate_cash_component=Decimal("1600"),
                safe_mid_fx=Decimal("6.8"),
                xop_close=Decimal("158"),
                q_nav=Decimal("995"),
                q_net=Decimal("995"),
                chosen_q=Decimal("995"),
                chosen_method="pcf_net",
            )
            tab.calibration_store.append_or_replace_pcf_point(point)
            tab.query_date.setDate(pcf_tab._qdate(date(2026, 7, 3)))
            tab.calculate_selected_date(save_point=False)
            date_values = {
                tab.date_estimate_table.item(row, 0).text(): tab.date_estimate_table.item(row, 1).text()
                for row in range(tab.date_estimate_table.rowCount())
            }
            self.assertEqual(date_values["本次到账预估采用的每申赎单位XOP股数"], "995.0000")
            self.assertEqual(date_values["预估ETF申购退款（补券退款）"], "1,065,883.80")

            tab.manual_shares_check.setChecked(True)
            tab.manual_shares_spin.setValue(998)
            tab.calculate_selected_date(save_point=False)
            manual_values = {
                tab.date_estimate_table.item(row, 0).text(): tab.date_estimate_table.item(row, 1).text()
                for row in range(tab.date_estimate_table.rowCount())
            }
            self.assertEqual(manual_values["本次到账预估采用的每申赎单位XOP股数"], "998.0000")
            self.assertEqual(manual_values["预估ETF申购退款（补券退款）"], "1,069,097.52")
            self.assertEqual(tab.calibration_store.point_for_day(date(2026, 7, 3)).chosen_q, Decimal("995"))

            tab.actual_query_date.setDate(pcf_tab._qdate(date(2026, 7, 3)))
            tab.external_refund.setValue(1_071_240)
            tab.calculate_external_validation()
            actual_values = {
                tab.actual_validation_table.item(row, 0).text(): tab.actual_validation_table.item(row, 1).text()
                for row in range(tab.actual_validation_table.rowCount())
            }
            self.assertEqual(actual_values["根据实际ETF申购退款反推的每申赎单位XOP股数"], "1,000.0000")
            self.assertEqual(actual_values["实际到账反推股数与PCF校准股数的差异"], "5.0000")
            tab.close()
            pcf_tab.close()

    def test_write_pcf_point_always_uses_target_159518_detail(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = {
                "fx_rates_csv_path": str(root / "fx.csv"),
                "xop_price_csv_path": str(root / "xop.csv"),
                "calibration_csv_path": str(root / "points.csv"),
                "settlement_observation_csv_path": str(root / "observations.csv"),
                "estimate_price_window": "1540_1600",
                "tws_host": "127.0.0.1",
                "tws_port": 7496,
                "tws_client_id": 8888,
            }
            pcf_tab = SzsePcfTab(root / "pcf", root / "fx.csv")
            selected_day = date(2026, 7, 9)
            pcf_tab.current_detail = szse_pcf.PcfDetail(
                item=pcf_tab.store.build_fund_item(selected_day, "159605"),
                metadata={"PreTradingDay": "20260708"},
                components=(),
                xml_path=None,
                txt_path=None,
                raw_text="",
            )
            tab = ArrivalCalibrationTab(config, pcf_tab)

            with (
                patch.object(pcf_tab.store, "ensure_target_detail", side_effect=RuntimeError("target path")) as ensure,
                patch("redemption_ui.QMessageBox.critical") as critical,
            ):
                tab.write_pcf_point()

            ensure.assert_called_once_with(selected_day)
            critical.assert_called_once()
            tab.close()
            pcf_tab.close()


if __name__ == "__main__":
    unittest.main()
