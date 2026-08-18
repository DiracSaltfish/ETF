from __future__ import annotations

import tempfile
import unittest
import json
import sys
import threading
import time
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType, SimpleNamespace

import fx_rates
import xop_close_orders
from realtime_premium import (
    TwsXopMarketData,
    automatic_tws_client_id,
    CfetsQuote,
    QuoteLevel,
    SinaQuote,
    XopQuote,
    calculate_premium_valuation,
    is_auto_connection_window,
    latest_cfets_quote,
    parse_sina_quote,
    tws_client_id_candidates,
    write_realtime_files,
)
from unittest.mock import patch


class RealtimePremiumTest(unittest.TestCase):
    def test_auto_connection_window_is_limited_to_domestic_session(self) -> None:
        self.assertFalse(is_auto_connection_window(datetime(2026, 7, 3, 9, 14, 59)))
        self.assertTrue(is_auto_connection_window(datetime(2026, 7, 3, 9, 15, 0)))
        self.assertTrue(is_auto_connection_window(datetime(2026, 7, 3, 14, 59, 59)))
        self.assertFalse(is_auto_connection_window(datetime(2026, 7, 3, 15, 0, 0)))
        self.assertFalse(is_auto_connection_window(datetime(2026, 7, 4, 10, 0, 0)))

    def test_ib_api_message_classification_keeps_order_and_connection_warnings(self) -> None:
        classify = TwsXopMarketData._classify_ib_api_message
        self.assertEqual(classify(326, "Unable to connect as the client id is already in use."), "client_id_in_use")
        self.assertEqual(
            classify(399, "Order message: will not be transmitted to the exchange until 09:30:00 US/Eastern"),
            "preopen_order_notice",
        )
        self.assertEqual(classify(399, "Order message error: invalid condition"), "error")
        self.assertEqual(classify(1100, "Connectivity between IB and TWS has been lost."), "connection_lost")
        self.assertEqual(
            classify(1102, "Connectivity between IB and TWS has been restored - data maintained."),
            "connection_restored_data_maintained",
        )
        self.assertEqual(classify(2103, "A market data farm is disconnected."), "farm_disconnected")

    def test_tws_client_id_candidates_support_auto_and_manual_first_modes(self) -> None:
        self.assertEqual(automatic_tws_client_id(4321), 104321)
        self.assertEqual(
            tws_client_id_candidates(8888, process_id=4321, attempts=4),
            (104321, 104322, 104323, 104324),
        )
        self.assertEqual(
            tws_client_id_candidates(
                8888,
                auto_allocate=False,
                process_id=4321,
                attempts=4,
            ),
            (8888, 104321, 104322, 104323),
        )

    def test_connecting_status_signal_is_emitted_after_releasing_lock(self) -> None:
        class AliveThread:
            @staticmethod
            def is_alive() -> bool:
                return True

        client = TwsXopMarketData("127.0.0.1", 7496, 8888)
        client._thread = AliveThread()  # type: ignore[assignment]
        lock_states: list[bool] = []
        client.statusChanged.connect(lambda *_args: lock_states.append(client._lock.locked()))

        client.connect_tws()

        self.assertEqual(lock_states, [False])

    def test_tws_connection_retries_immediately_after_client_id_326(self) -> None:
        attempts: list[int] = []

        class FakeSignal:
            def __init__(self) -> None:
                self.handlers = []

            def __iadd__(self, handler):
                self.handlers.append(handler)
                return self

            def __isub__(self, handler):
                self.handlers.remove(handler)
                return self

            def emit(self, *args) -> None:
                for handler in tuple(self.handlers):
                    handler(*args)

        class FakeClient:
            def __init__(self, owner) -> None:
                self.owner = owner
                self.ready = False
                self.apiStart = FakeSignal()

            def connect(self, _host, _port, client_id, timeout=0) -> None:
                del timeout
                attempts.append(client_id)
                if len(attempts) == 1:
                    self.owner.errorEvent.emit(
                        -1,
                        326,
                        "Unable to connect as the client id is already in use.",
                        None,
                    )
                    return
                self.ready = True

            def isConnected(self) -> bool:
                return self.ready

            def disconnect(self) -> None:
                self.ready = False

        class FakeIB:
            def __init__(self) -> None:
                self.errorEvent = FakeSignal()
                self.openOrderEvent = FakeSignal()
                self.orderStatusEvent = FakeSignal()
                self.execDetailsEvent = FakeSignal()
                self.commissionReportEvent = FakeSignal()
                self.wrapper = SimpleNamespace(clientId=None)
                self.client = FakeClient(self)
                self.RequestTimeout = 0

            def isConnected(self) -> bool:
                return self.client.ready

            def disconnect(self) -> None:
                self.client.ready = False

            @staticmethod
            def trades() -> list:
                return []

            @staticmethod
            def qualifyContracts(contract) -> list:
                return [contract]

            @staticmethod
            def reqMarketDataType(_market_data_type: int) -> None:
                return None

            @staticmethod
            def reqMktData(_contract, *_args):
                return SimpleNamespace(
                    bid=float("nan"),
                    ask=float("nan"),
                    last=float("nan"),
                    marketDataType=1,
                )

            @staticmethod
            def reqAllOpenOrders() -> list:
                return []

            @staticmethod
            def sleep(_seconds: float) -> None:
                time.sleep(0.001)

            @staticmethod
            def cancelMktData(_contract) -> None:
                return None

        fake_module = ModuleType("ib_insync")
        fake_module.IB = FakeIB  # type: ignore[attr-defined]
        client = TwsXopMarketData("127.0.0.1", 7496, 8888, auto_client_id=True)
        statuses: list[str] = []
        client._emit_status = lambda text, _active: statuses.append(text)  # type: ignore[method-assign]

        with (
            patch("realtime_premium.os.getpid", return_value=4321),
            patch.dict(sys.modules, {"ib_insync": fake_module}),
            patch.object(client, "build_contract", return_value=SimpleNamespace()),
        ):
            client.connect_tws()
            thread = client._thread
            self.assertIsNotNone(thread)
            deadline = time.monotonic() + 2
            while not client.is_connected() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(
                client.is_connected(),
                {"statuses": statuses, "attempts": attempts, "thread_alive": thread.is_alive()},
            )
            self.assertEqual(client.active_client_id, 104322)
            self.assertEqual(attempts, [104321, 104322])
            client.disconnect_tws()
            thread.join(timeout=2)  # type: ignore[union-attr]
            self.assertFalse(thread.is_alive())  # type: ignore[union-attr]

    def test_calculation_uses_total_asset_model_and_separate_sides(self) -> None:
        value = calculate_premium_valuation(
            Decimal("150"),
            Decimal("150.1"),
            Decimal("7"),
            Decimal("1.050"),
            Decimal("1.051"),
            estimate_cash_component_cny=Decimal("1500"),
        )
        self.assertEqual(value.shares, Decimal("996"))
        self.assertEqual(value.stock_component_bid_cny, Decimal("1045800"))
        self.assertEqual(value.basket_bid_cny, Decimal("1047300"))
        self.assertEqual(value.basket_ask_cny, Decimal("1047997.2"))
        self.assertEqual(value.nav_bid, Decimal("1.0473"))
        self.assertEqual(value.nav_ask, Decimal("1.0479972"))
        self.assertEqual(value.domestic_bid_vs_xop_bid, Decimal("1.050") / Decimal("1.0473") - 1)
        self.assertEqual(value.domestic_ask_vs_xop_ask, Decimal("1.051") / Decimal("1.0479972") - 1)

    def test_sina_payload_parses_best_bid_and_ask(self) -> None:
        fields = ["标普油气ETF", "1.000", "1.001", "1.002", "1.003", "0.999", "0", "0", "100", "1000"]
        fields += ["120000", "1.001", "0", "0", "0", "0", "0", "0", "0", "0"]
        fields += ["80000", "1.002", "0", "0", "0", "0", "0", "0", "0", "0"]
        fields += ["2026-07-03", "15:00:00", "00"]
        payload = f'var hq_str_sz159518="{",".join(fields)}";'.encode("gb18030")
        quote = parse_sina_quote(payload, received_at=datetime(2026, 7, 3, 15, 0, 1))
        self.assertEqual(quote.bid, Decimal("1.001"))
        self.assertEqual(quote.ask, Decimal("1.002"))
        self.assertEqual(quote.bid_volume, 120000)
        self.assertEqual(quote.ask_volume, 80000)
        self.assertEqual(quote.market_time, datetime(2026, 7, 3, 15, 0, 0))
        self.assertEqual(len(quote.bids), 5)
        self.assertEqual(len(quote.asks), 5)
        self.assertEqual(quote.bids[0].price, Decimal("1.001"))
        self.assertEqual(quote.asks[0].price, Decimal("1.002"))

    def test_latest_cfets_quote_uses_latest_hour_and_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "fx.csv"
            csv_path.write_text(
                ",".join(fx_rates.CSV_FIELDS) + "\n"
                "2026-07-03,CFETS_REFERENCE_RATE,USD/CNY,USD/CNY,16:00,6.79,6.79,pair_standard,pair_standard,now,\n"
                "2026-07-03,CFETS_REFERENCE_RATE,USD/CNY,USD/CNY,18:00,6.80,6.80,pair_standard,pair_standard,now,\n",
                encoding="utf-8",
            )
            store = fx_rates.FxRateStore(csv_path)
            quote = latest_cfets_quote(store, as_of=date(2026, 7, 5), refresh=False)
            self.assertEqual(quote.trading_day, date(2026, 7, 3))
            self.assertEqual(quote.quote_time, "18:00")
            self.assertEqual(quote.rate, Decimal("6.80"))

    def test_tws_contract_matches_reference_program(self) -> None:
        contract = TwsXopMarketData.build_contract()
        self.assertEqual(contract.conId, 413951498)
        self.assertEqual(contract.symbol, "XOP")
        self.assertEqual(contract.exchange, "SMART")
        self.assertEqual(contract.primaryExchange, "ARCA")
        self.assertEqual(contract.currency, "USD")

    def test_disconnect_clears_confirmed_but_unsent_order_queue(self) -> None:
        client = TwsXopMarketData("127.0.0.1", 7496, 8888)
        events: list[dict[str, object]] = []
        client.orderEvent.connect(events.append)
        spec = xop_close_orders.generate_order_specs(date(2030, 1, 7), 990)[0]
        with patch.object(client, "is_connected", return_value=True):
            self.assertTrue(client.submit_confirmed_order(spec))
        client.disconnect_tws()
        self.assertEqual([item["event"] for item in events], ["queued", "rejected"])
        self.assertIn("已清除", str(events[-1]["message"]))
        self.assertTrue(client._order_queue.empty())

    def test_formatted_shared_json_contains_four_prices_without_volumes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime.fromisoformat("2026-07-06T09:31:02+08:00")
            xop = XopQuote(Decimal("150"), Decimal("150.1"), Decimal("150.05"), now, "Live")
            domestic = SinaQuote(
                "159518", "标普油气", Decimal("1.050"), Decimal("1.051"),
                100_000, 80_000, Decimal("1.050"), now.replace(tzinfo=None), now.replace(tzinfo=None),
                bids=(QuoteLevel(Decimal("1.050"), 100_000), QuoteLevel(Decimal("1.049"), 90_000)),
                asks=(QuoteLevel(Decimal("1.051"), 80_000), QuoteLevel(Decimal("1.052"), 70_000)),
            )
            cfets = CfetsQuote(Decimal("7"), date(2026, 7, 6), "10:00", "now")
            valuation = calculate_premium_valuation(
                xop.bid,
                xop.ask,
                cfets.rate,
                domestic.bid,
                domestic.ask,
                estimate_cash_component_cny=Decimal("1234.56"),
                pcf_trading_day=date(2026, 7, 6),
            )
            json_path, document_path = write_realtime_files(
                temp_dir, xop, domestic, cfets, valuation, generated_at=now
            )
            self.assertEqual(json_path, Path(temp_dir).resolve() / "20260706" / "realtime.json")
            self.assertTrue(document_path.exists())
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            self.assertEqual(list(payload["order_book"]), ["bid2", "bid1", "ask1", "ask2"])
            self.assertEqual(payload["order_book"]["bid2"]["price"], 1.049)
            self.assertEqual(payload["order_book"]["ask2"]["price"], 1.052)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["valuation"]["pcf"]["estimate_cash_component_cny"], 1234.56)
            self.assertEqual(payload["valuation"]["pcf"]["trading_day"], "2026-07-06")
            self.assertEqual(payload["valuation"]["basket"]["bid_cny"], 1047034.56)
            self.assertNotIn("volume", json_path.read_text(encoding="utf-8"))
            self.assertIn("premium_rate_vs_xop_bid_nav", payload["order_book"]["bid1"])
            self.assertIn("premium_rate_vs_xop_ask_nav", payload["order_book"]["ask1"])
            self.assertIn("premium_rate_vs_basket_bid_nav", payload["order_book"]["bid1"])
            self.assertIn("premium_rate_vs_basket_ask_nav", payload["order_book"]["ask1"])

    def test_auction_one_level_quote_exports_cpp_compatible_numbers(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            now = datetime.fromisoformat("2026-07-10T09:22:33+08:00")
            xop = XopQuote(Decimal("159.02"), Decimal("159.70"), Decimal("159.36"), now, "Live")
            domestic = SinaQuote(
                "159518", "标普油气", Decimal("1.070"), Decimal("1.070"),
                460_100, 460_100, Decimal("0"), now.replace(tzinfo=None), now.replace(tzinfo=None),
                bids=(QuoteLevel(Decimal("1.070"), 460_100), QuoteLevel(Decimal("0"), 0)),
                asks=(QuoteLevel(Decimal("1.070"), 460_100), QuoteLevel(Decimal("0"), 0)),
            )
            cfets = CfetsQuote(Decimal("6.794"), date(2026, 7, 10), "18:00", "now")
            valuation = calculate_premium_valuation(
                xop.bid, xop.ask, cfets.rate, domestic.bid, domestic.ask
            )
            json_path, _document_path = write_realtime_files(
                temp_dir, xop, domestic, cfets, valuation, generated_at=now
            )
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            book = payload["order_book"]
            self.assertEqual(book["bid2"]["price"], 1.07)
            self.assertEqual(book["bid1"]["price"], 1.07)
            self.assertEqual(book["ask1"]["price"], 1.07)
            self.assertEqual(book["ask2"]["price"], 1.07)
            for key in ("bid2", "bid1", "ask1", "ask2"):
                self.assertIsInstance(book[key]["price"], float)
                self.assertIsInstance(book[key]["premium_percent_vs_xop_bid_nav"], float)
                self.assertIsInstance(book[key]["premium_percent_vs_xop_ask_nav"], float)


if __name__ == "__main__":
    unittest.main()
