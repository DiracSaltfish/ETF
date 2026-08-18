from __future__ import annotations

from datetime import date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from india_config import IndiaConfig
from india_order_planner import (
    build_ib_order,
    build_inda_close_plan,
    build_swap_plan,
    is_swap_batch,
    validate_live_plan,
    validate_qualified_contract,
)
from india_store import IndiaStore
from india_tws_orders import IndiaTwsOrderClient


def live_config() -> IndiaConfig:
    return IndiaConfig(live_enabled=True)


def test_live_validation_requires_switch_and_future_trigger() -> None:
    future = build_inda_close_plan(date(2026, 7, 6), 970, live_config())
    before_open = datetime(2026, 7, 6, 9, 0, tzinfo=ZoneInfo("America/New_York"))
    assert validate_live_plan(live_config(), future, now=before_open)
    with pytest.raises(ValueError, match="实盘总开关"):
        validate_live_plan(IndiaConfig(), build_inda_close_plan(date(2099, 7, 6), 970, IndiaConfig()))
    past = build_inda_close_plan(date(2026, 7, 6), 970, live_config())
    with pytest.raises(ValueError, match="触发时间已经过去"):
        validate_live_plan(
            live_config(),
            past,
            now=datetime(2026, 7, 6, 16, 0, tzinfo=ZoneInfo("America/New_York")),
        )


def test_live_validation_blocks_nyse_holiday_and_early_close() -> None:
    holiday = build_inda_close_plan(date(2026, 7, 3), 970, live_config())
    with pytest.raises(ValueError, match="休市日"):
        validate_live_plan(
            live_config(),
            holiday,
            now=datetime(2026, 7, 3, 9, 0, tzinfo=ZoneInfo("America/New_York")),
        )
    early_close = build_inda_close_plan(date(2026, 11, 27), 970, live_config())
    with pytest.raises(ValueError, match="提前收盘"):
        validate_live_plan(
            live_config(),
            early_close,
            now=datetime(2026, 11, 27, 9, 0, tzinfo=ZoneInfo("America/New_York")),
        )


def test_swap_is_complete_parent_child_batch() -> None:
    specs = build_swap_plan(date(2099, 7, 6), 1, live_config())
    assert is_swap_batch(specs)
    assert not is_swap_batch(specs[:1])
    parent = build_ib_order(specs[0], account="U123", transmit=False)
    child = build_ib_order(
        specs[1], account="U123", parent_id=123, include_time_condition=False
    )
    assert parent.transmit is False
    assert parent.account == "U123"
    assert len(parent.conditions) == 1
    assert child.parentId == 123
    assert child.transmit is True
    assert child.conditions == []


def test_qualified_contract_identity_is_strict() -> None:
    spec = build_swap_plan(date(2026, 7, 6), 1, live_config())[0]
    good = SimpleNamespace(
        conId=123,
        symbol="NIFTY",
        localSymbol=spec.symbol,
        secType="FUT",
        currency="USD",
        lastTradeDateOrContractMonth="20260728",
        multiplier="2",
    )
    validate_qualified_contract(spec, good)
    wrong_month = SimpleNamespace(**{**vars(good), "lastTradeDateOrContractMonth": "20260825"})
    with pytest.raises(ValueError, match="合约月"):
        validate_qualified_contract(spec, wrong_month)


def test_position_guard_blocks_reverse_inda_long() -> None:
    specs = build_inda_close_plan(date(2099, 7, 6), 970, live_config())
    contracts = [SimpleNamespace(conId=88), SimpleNamespace(conId=88)]
    fake_ib = SimpleNamespace(
        positions=lambda: [
            SimpleNamespace(account="U123", contract=SimpleNamespace(conId=88), position=-900)
        ]
    )
    client = IndiaTwsOrderClient("127.0.0.1", 7496, 8888, account="U123")
    client.active_account = "U123"
    with pytest.raises(ValueError, match="阻止反向开多"):
        client._validate_positions(fake_ib, specs, contracts)


def test_tws_queue_rejects_duplicate_and_partial_swap(monkeypatch) -> None:
    client = IndiaTwsOrderClient("127.0.0.1", 7496, 8888)
    monkeypatch.setattr(client, "is_connected", lambda: True)
    specs = build_swap_plan(date(2099, 7, 6), 1, live_config())
    assert client.submit_confirmed_batch(specs)
    assert not client.submit_confirmed_batch(specs)
    other = build_swap_plan(date(2099, 7, 7), 1, live_config())
    assert not client.submit_confirmed_batch(other[:1])


def test_cancel_request_is_processed_on_tws_thread(monkeypatch) -> None:
    client = IndiaTwsOrderClient("127.0.0.1", 7496, 8888)
    monkeypatch.setattr(client, "is_connected", lambda: True)
    order = SimpleNamespace(orderRef="INDIA_TEST_CANCEL", orderId=41)
    trade = SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PreSubmitted"))
    cancelled = []
    fake_ib = SimpleNamespace(
        openTrades=lambda: [trade],
        reqAllOpenOrders=lambda: [],
        cancelOrder=lambda value: cancelled.append(value),
    )
    events = []
    client.orderEvent.connect(events.append)
    assert client.request_cancel(["INDIA_TEST_CANCEL"])
    client._process_cancel_requests(fake_ib)
    assert cancelled == [order]
    assert any(item["event"] == "cancelRequested" for item in events)


def test_independent_close_reports_partial_submission() -> None:
    client = IndiaTwsOrderClient("127.0.0.1", 7496, 8888, account="U123")
    client.active_account = "U123"
    specs = build_inda_close_plan(date(2026, 7, 6), 970, live_config())
    contract = SimpleNamespace(
        conId=88,
        symbol="INDA",
        localSymbol="INDA",
        secType="STK",
        currency="USD",
    )
    call_count = 0

    def place_order(_contract, order):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RuntimeError("second failed")
        order.orderId = 51
        return SimpleNamespace(order=order, orderStatus=SimpleNamespace(status="PreSubmitted"))

    fake_ib = SimpleNamespace(
        qualifyContracts=lambda *_contracts: [contract, contract],
        positions=lambda: [
            SimpleNamespace(account="U123", contract=SimpleNamespace(conId=88), position=-970)
        ],
        placeOrder=place_order,
    )
    events = []
    client.orderEvent.connect(events.append)
    client._submit_batch(fake_ib, specs)
    assert [item["event"] for item in events] == ["submitted", "rejected"]


def test_order_events_are_persisted(tmp_path) -> None:
    store = IndiaStore(tmp_path / "india.sqlite3")
    store.append_order_event(
        {
            "event": "submitted",
            "order_ref": "INDIA_TEST_1",
            "order_id": 17,
            "status": "PreSubmitted",
            "message": "test",
        }
    )
    rows = store.list_order_events("INDIA_TEST_1")
    assert len(rows) == 1
    assert rows[0]["event_type"] == "submitted"
    assert rows[0]["order_id"] == 17
    assert store.sent_order_refs() == {"INDIA_TEST_1"}


def test_cancelled_order_gets_new_revision_ref(tmp_path) -> None:
    store = IndiaStore(tmp_path / "india.sqlite3")
    spec = build_inda_close_plan(date(2026, 7, 6), 970, live_config())[0]
    store.save_order_specs([spec])
    store.append_order_event({"event": "submitted", "order_ref": spec.order_ref, "order_id": 1})
    store.append_order_event(
        {"event": "orderStatus", "order_ref": spec.order_ref, "order_id": 1, "status": "Cancelled"}
    )
    replacement = store.next_order_ref(spec.order_ref)
    assert replacement == spec.order_ref + "_R2"
    revised = SimpleNamespace(**{**vars(spec), "order_ref": replacement})
    store.save_order_specs([revised])
    assert store.next_order_ref(spec.order_ref) == replacement


def test_redemption_can_be_deleted_for_correction(tmp_path) -> None:
    from india_models import RedemptionEvent

    store = IndiaStore(tmp_path / "india.sqlite3")
    event = RedemptionEvent("manual:test", "QMT1", date(2026, 7, 6), 270000)
    store.upsert_redemption(event)
    assert store.delete_redemption(event.event_id)
    assert store.list_redemptions() == []
    assert not store.delete_redemption(event.event_id)
