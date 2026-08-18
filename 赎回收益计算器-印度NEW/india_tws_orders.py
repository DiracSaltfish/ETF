from __future__ import annotations

import queue
import threading
from typing import Iterable

from PyQt5.QtCore import QObject, pyqtSignal

from india_models import IndiaOrderSpec
from india_order_planner import (
    build_ib_contract,
    build_ib_order,
    is_swap_batch,
    validate_qualified_contract,
)
from realtime_premium import tws_client_id_candidates


class IndiaTwsOrderClient(QObject):
    """Background TWS sender dedicated to INDIA_ strategy orders."""

    statusChanged = pyqtSignal(str, bool)
    orderEvent = pyqtSignal(object)

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        *,
        account: str = "",
        auto_client_id: bool = True,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.host = host
        self.port = int(port)
        self.client_id = int(client_id)
        self.account = account.strip()
        self.auto_client_id = bool(auto_client_id)
        self.active_client_id: int | None = None
        self.active_account = ""
        self._lock = threading.Lock()
        self._ib = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._batch_queue: queue.Queue[tuple[IndiaOrderSpec, ...]] = queue.Queue()
        self._cancel_queue: queue.Queue[tuple[str, ...]] = queue.Queue()
        self._submitted_order_refs: set[str] = set()
        self._queued_order_refs: set[str] = set()

    def seed_submitted_order_refs(self, order_refs: Iterable[str]) -> None:
        self._submitted_order_refs.update(
            str(item) for item in order_refs if str(item).startswith("INDIA_")
        )

    def configure(
        self,
        *,
        host: str,
        port: int,
        client_id: int,
        account: str,
        auto_client_id: bool,
    ) -> None:
        if self.is_connected() or (self._thread is not None and self._thread.is_alive()):
            raise RuntimeError("请先断开IB，再修改TWS连接配置")
        self.host = str(host)
        self.port = int(port)
        self.client_id = int(client_id)
        self.account = str(account).strip()
        self.auto_client_id = bool(auto_client_id)

    def is_connected(self) -> bool:
        with self._lock:
            ib = self._ib
        return bool(ib is not None and ib.isConnected())

    def is_busy(self) -> bool:
        thread = self._thread
        return bool(thread is not None and thread.is_alive())

    def _emit_status(self, text: str, connected: bool) -> None:
        try:
            self.statusChanged.emit(text, connected)
        except RuntimeError:
            pass

    def _emit_order_event(self, payload: dict[str, object]) -> None:
        try:
            self.orderEvent.emit(payload)
        except RuntimeError:
            pass

    def submit_confirmed_batch(self, specs: Iterable[IndiaOrderSpec]) -> bool:
        items = tuple(specs)
        if not items:
            return False
        if not self.is_connected():
            self._reject_batch(items, "IB未连接，订单未发送")
            return False
        refs = [item.order_ref for item in items]
        if len(refs) != len(set(refs)):
            self._reject_batch(items, "批次内 orderRef 重复，订单未发送")
            return False
        duplicate = next(
            (
                ref
                for ref in refs
                if ref in self._submitted_order_refs or ref in self._queued_order_refs
            ),
            "",
        )
        if duplicate:
            self._reject_batch(items, f"{duplicate} 已存在，禁止重复发送")
            return False
        if any(ref.startswith("INDIA_SWAP_") for ref in refs) and not is_swap_batch(items):
            self._reject_batch(items, "换仓必须同时提交完整的 NIFTY父单 + INDA子单")
            return False
        self._queued_order_refs.update(refs)
        self._batch_queue.put(items)
        for item in items:
            self._emit_order_event(
                {"event": "queued", "order_ref": item.order_ref, "message": "已进入TWS发送队列"}
            )
        return True

    def request_cancel(self, order_refs: Iterable[str]) -> bool:
        refs = tuple(dict.fromkeys(str(item) for item in order_refs if str(item).startswith("INDIA_")))
        if not refs:
            return False
        if not self.is_connected():
            for order_ref in refs:
                self._emit_order_event(
                    {"event": "cancelRejected", "order_ref": order_ref, "message": "IB未连接，无法撤单"}
                )
            return False
        self._cancel_queue.put(refs)
        for order_ref in refs:
            self._emit_order_event(
                {"event": "cancelQueued", "order_ref": order_ref, "message": "撤单请求已进入TWS队列"}
            )
        return True

    def _reject_batch(self, specs: Iterable[IndiaOrderSpec], message: str) -> None:
        for item in specs:
            self._emit_order_event(
                {"event": "rejected", "order_ref": item.order_ref, "message": message}
            )

    def connect_tws(self) -> None:
        candidates = tws_client_id_candidates(self.client_id, auto_allocate=self.auto_client_id)

        def run() -> None:
            ib = None
            event_loop = None
            terminal_error = ""
            try:
                import asyncio
                from ib_insync import IB

                event_loop = asyncio.new_event_loop()
                asyncio.set_event_loop(event_loop)
                for attempt, candidate_id in enumerate(candidates, start=1):
                    if self._stop_event.is_set():
                        return
                    candidate = IB()
                    conflict = threading.Event()

                    def on_connect_error(
                        _req_id: int,
                        error_code: int,
                        _error_string: str,
                        _contract,
                        *,
                        attempted_id: int = candidate_id,
                        attempted_ib=candidate,
                    ) -> None:
                        if error_code != 326:
                            return
                        conflict.set()
                        self._emit_status(f"IB client ID {attempted_id} 已占用，正在自动切换", False)
                        try:
                            attempted_ib.client.apiStart.emit()
                        except Exception:
                            pass

                    candidate.errorEvent += on_connect_error
                    with self._lock:
                        self._ib = candidate
                    if attempt > 1:
                        self._emit_status(
                            f"正在重试IB {self.host}:{self.port}，client ID {candidate_id}", False
                        )
                    try:
                        candidate.wrapper.clientId = candidate_id
                        candidate.client.connect(self.host, self.port, candidate_id, timeout=8)
                    except Exception:
                        if not conflict.is_set():
                            raise
                    finally:
                        candidate.errorEvent -= on_connect_error
                    if conflict.is_set():
                        try:
                            candidate.client.disconnect()
                        except Exception:
                            pass
                        with self._lock:
                            if self._ib is candidate:
                                self._ib = None
                        continue
                    if not candidate.isConnected():
                        raise RuntimeError("TWS未建立连接")
                    ib = candidate
                    self.active_client_id = candidate_id
                    break
                if ib is None:
                    raise RuntimeError(f"连续尝试 {len(candidates)} 个 client ID 均未连接")

                self.active_account = self._resolve_account(ib)
                self._attach_events(ib)
                self._load_existing_order_refs(ib)
                account_text = self.active_account or "TWS默认账户"
                self._emit_status(
                    f"IB交易接口已连接（client ID {self.active_client_id}，账户 {account_text}）", True
                )
                while ib.isConnected() and not self._stop_event.is_set():
                    ib.sleep(0.1)
                    self._process_cancel_requests(ib)
                    self._process_next_batch(ib)
            except Exception as exc:
                terminal_error = str(exc)
                self._emit_status(f"IB连接失败：{exc}", False)
            finally:
                with self._lock:
                    tracked = self._ib
                if tracked is not None:
                    try:
                        tracked.client.disconnect()
                    except Exception:
                        pass
                with self._lock:
                    if self._ib is tracked:
                        self._ib = None
                    self._thread = None
                self.active_client_id = None
                self.active_account = ""
                self._clear_queue("IB连接已结束，排队订单已清除")
                self._emit_status("IB连接失败后已断开" if terminal_error else "IB已断开", False)
                if event_loop is not None:
                    event_loop.close()

        thread = threading.Thread(target=run, name="tws-india-orders", daemon=True)
        with self._lock:
            busy = self._thread is not None and self._thread.is_alive()
            if not busy:
                self._thread = thread
        if busy:
            self._emit_status("IB已经连接或正在连接", self.is_connected())
            return
        self._stop_event.clear()
        self._emit_status(
            f"正在连接IB {self.host}:{self.port}，client ID {candidates[0]}", False
        )
        thread.start()

    def _resolve_account(self, ib) -> str:
        accounts = [str(item) for item in ib.managedAccounts() if str(item)]
        if self.account:
            if accounts and self.account not in accounts:
                raise ValueError(f"配置账户 {self.account} 不在TWS可用账户列表中")
            return self.account
        if len(accounts) > 1:
            raise ValueError("TWS包含多个账户；请在数据源设置中明确选择交易账户")
        return accounts[0] if accounts else ""

    def _attach_events(self, ib) -> None:
        def emit_trade_event(event_name: str, trade, message: str = "") -> None:
            order_ref = str(getattr(trade.order, "orderRef", "") or "")
            if not order_ref.startswith("INDIA_"):
                return
            self._emit_order_event(
                {
                    "event": event_name,
                    "order_ref": order_ref,
                    "order_id": int(getattr(trade.order, "orderId", 0) or 0),
                    "perm_id": int(getattr(trade.order, "permId", 0) or 0),
                    "status": str(getattr(trade.orderStatus, "status", "") or ""),
                    "filled": float(getattr(trade.orderStatus, "filled", 0) or 0),
                    "remaining": float(getattr(trade.orderStatus, "remaining", 0) or 0),
                    "message": message,
                }
            )

        ib.openOrderEvent += lambda trade: emit_trade_event("openOrder", trade)
        ib.orderStatusEvent += lambda trade: emit_trade_event("orderStatus", trade)
        ib.execDetailsEvent += lambda trade, fill: emit_trade_event(
            "execDetails", trade, f"execution={getattr(fill.execution, 'execId', '')}"
        )
        ib.commissionReportEvent += lambda trade, fill, report: emit_trade_event(
            "commissionReport", trade, f"commission={getattr(report, 'commission', '')}"
        )

        def on_error(req_id: int, error_code: int, error_string: str, _contract) -> None:
            if error_code in {10275, 2104, 2106, 2107, 2108, 2158}:
                return
            matching = next(
                (
                    trade
                    for trade in ib.trades()
                    if int(getattr(trade.order, "orderId", 0) or 0) == int(req_id)
                ),
                None,
            )
            order_ref = str(getattr(matching.order, "orderRef", "") or "") if matching else ""
            if order_ref and not order_ref.startswith("INDIA_"):
                return
            self._emit_order_event(
                {
                    "event": "error" if error_code not in {1101, 1102} else "warning",
                    "order_ref": order_ref,
                    "order_id": req_id,
                    "message": f"IB错误 {error_code}：{error_string}",
                }
            )

        ib.errorEvent += on_error

    def _load_existing_order_refs(self, ib) -> None:
        previous_timeout = ib.RequestTimeout
        try:
            ib.RequestTimeout = 5
            trades = list(ib.reqAllOpenOrders())
            try:
                trades.extend(ib.reqCompletedOrders(False))
            except Exception as exc:
                self._emit_order_event(
                    {"event": "warning", "order_ref": "", "message": f"读取TWS已完成订单失败：{exc}"}
                )
            seen: set[str] = set()
            for trade in trades:
                order_ref = str(getattr(trade.order, "orderRef", "") or "")
                if not order_ref.startswith("INDIA_") or order_ref in seen:
                    continue
                seen.add(order_ref)
                self._submitted_order_refs.add(order_ref)
                self._emit_order_event(
                    {
                        "event": "sync",
                        "order_ref": order_ref,
                        "order_id": int(getattr(trade.order, "orderId", 0) or 0),
                        "perm_id": int(getattr(trade.order, "permId", 0) or 0),
                        "status": str(getattr(trade.orderStatus, "status", "") or ""),
                        "filled": float(getattr(trade.orderStatus, "filled", 0) or 0),
                        "remaining": float(getattr(trade.orderStatus, "remaining", 0) or 0),
                        "message": "已从TWS同步订单状态",
                    }
                )
        except Exception as exc:
            self._emit_order_event(
                {"event": "warning", "order_ref": "", "message": f"读取现有印度条件单失败：{exc}"}
            )
        finally:
            ib.RequestTimeout = previous_timeout

    def _process_cancel_requests(self, ib) -> None:
        try:
            refs = self._cancel_queue.get_nowait()
        except queue.Empty:
            return
        try:
            trades = list(ib.openTrades())
            known = {
                str(getattr(trade.order, "orderRef", "") or ""): trade
                for trade in trades
                if str(getattr(trade.order, "orderRef", "") or "").startswith("INDIA_")
            }
            missing = [order_ref for order_ref in refs if order_ref not in known]
            if missing:
                for trade in ib.reqAllOpenOrders():
                    order_ref = str(getattr(trade.order, "orderRef", "") or "")
                    if order_ref in missing:
                        known[order_ref] = trade
            terminal = {"FILLED", "CANCELLED", "APICANCELLED", "INACTIVE"}
            for order_ref in refs:
                trade = known.get(order_ref)
                if trade is None:
                    self._emit_order_event(
                        {
                            "event": "cancelRejected",
                            "order_ref": order_ref,
                            "message": "TWS未找到该策略的未完成订单；请先同步核对",
                        }
                    )
                    continue
                status = str(getattr(trade.orderStatus, "status", "") or "")
                if status.upper() in terminal:
                    self._emit_order_event(
                        {
                            "event": "cancelRejected",
                            "order_ref": order_ref,
                            "order_id": int(getattr(trade.order, "orderId", 0) or 0),
                            "status": status,
                            "message": f"订单已处于终态 {status}，无需撤单",
                        }
                    )
                    continue
                ib.cancelOrder(trade.order)
                self._emit_order_event(
                    {
                        "event": "cancelRequested",
                        "order_ref": order_ref,
                        "order_id": int(getattr(trade.order, "orderId", 0) or 0),
                        "status": status,
                        "message": "撤单请求已发送到TWS，等待Cancelled回报",
                    }
                )
        except Exception as exc:
            for order_ref in refs:
                self._emit_order_event(
                    {"event": "cancelRejected", "order_ref": order_ref, "message": f"撤单失败：{exc}"}
                )

    def _process_next_batch(self, ib) -> None:
        try:
            specs = self._batch_queue.get_nowait()
        except queue.Empty:
            return
        try:
            self._submit_batch(ib, specs)
        except Exception as exc:
            self._reject_batch(specs, f"发送失败：{exc}")
        finally:
            self._queued_order_refs.difference_update(item.order_ref for item in specs)

    def _submit_batch(self, ib, specs: tuple[IndiaOrderSpec, ...]) -> None:
        for item in specs:
            if item.order_ref in self._submitted_order_refs:
                raise ValueError(f"{item.order_ref} 已存在，禁止重复发送")
        raw_contracts = [build_ib_contract(item) for item in specs]
        qualified = list(ib.qualifyContracts(*raw_contracts))
        if len(qualified) != len(specs):
            raise ValueError("TWS未能确认全部 NIFTY/INDA 合约，订单未发送")
        for spec, contract in zip(specs, qualified):
            validate_qualified_contract(spec, contract)
        self._validate_positions(ib, specs, qualified)
        if is_swap_batch(specs):
            self._submit_parent_child(ib, specs, qualified)
            return
        for spec, contract in zip(specs, qualified):
            try:
                order = build_ib_order(spec, account=self.active_account)
                trade = ib.placeOrder(contract, order)
            except Exception as exc:
                self._emit_order_event(
                    {
                        "event": "rejected",
                        "order_ref": spec.order_ref,
                        "message": f"该笔条件单发送失败：{exc}",
                    }
                )
                continue
            self._submitted_order_refs.add(spec.order_ref)
            self._emit_order_event(
                {
                    "event": "submitted",
                    "order_ref": spec.order_ref,
                    "order_id": int(getattr(trade.order, "orderId", 0) or 0),
                    "status": str(getattr(trade.orderStatus, "status", "") or ""),
                    "message": "条件单已发送到TWS",
                }
            )

    def _validate_positions(self, ib, specs, contracts) -> None:
        positions = [
            item
            for item in ib.positions()
            if not self.active_account or str(getattr(item, "account", "")) == self.active_account
        ]

        def current_position(contract) -> float:
            con_id = int(getattr(contract, "conId", 0) or 0)
            return sum(
                float(getattr(item, "position", 0) or 0)
                for item in positions
                if int(getattr(item.contract, "conId", 0) or 0) == con_id
            )

        if is_swap_batch(specs):
            nifty_position = current_position(contracts[0])
            required = specs[0].quantity
            if nifty_position > -required:
                raise ValueError(
                    f"NIFTY当前持仓 {nifty_position:g}，不足以用BUY {required}平掉空头"
                )
            return
        inda_buys = sum(
            spec.quantity
            for spec in specs
            if spec.symbol == "INDA" and spec.action == "BUY"
        )
        if inda_buys:
            inda_contract = next(
                contract for spec, contract in zip(specs, contracts) if spec.symbol == "INDA"
            )
            inda_position = current_position(inda_contract)
            if inda_position > -inda_buys:
                raise ValueError(
                    f"INDA当前持仓 {inda_position:g}，不足以用BUY {inda_buys}平仓；已阻止反向开多"
                )

    def _submit_parent_child(self, ib, specs, contracts) -> None:
        parent_spec, child_spec = specs
        parent_order = build_ib_order(
            parent_spec,
            account=self.active_account,
            transmit=False,
        )
        parent_trade = ib.placeOrder(contracts[0], parent_order)
        parent_id = int(getattr(parent_trade.order, "orderId", 0) or 0)
        if parent_id <= 0:
            raise RuntimeError("TWS未返回NIFTY父单订单号")
        try:
            ib.sleep(0.05)
            child_order = build_ib_order(
                child_spec,
                account=self.active_account,
                transmit=True,
                parent_id=parent_id,
                include_time_condition=False,
            )
            child_trade = ib.placeOrder(contracts[1], child_order)
        except Exception:
            try:
                ib.cancelOrder(parent_trade.order)
            except Exception:
                pass
            raise
        self._submitted_order_refs.update(item.order_ref for item in specs)
        child_id = int(getattr(child_trade.order, "orderId", 0) or 0)
        self._emit_order_event(
            {
                "event": "submitted",
                "order_ref": parent_spec.order_ref,
                "order_id": parent_id,
                "message": "NIFTY时间条件父单已发送；transmit由INDA子单原子触发",
            }
        )
        self._emit_order_event(
            {
                "event": "submitted",
                "order_ref": child_spec.order_ref,
                "order_id": child_id,
                "parent_id": parent_id,
                "message": "INDA子单已附加；仅在NIFTY父单完全成交后激活",
            }
        )

    def disconnect_tws(self) -> None:
        self._stop_event.set()
        self._clear_queue("IB已断开，排队订单已清除")

    def _clear_queue(self, message: str) -> None:
        while True:
            try:
                specs = self._batch_queue.get_nowait()
            except queue.Empty:
                break
            self._reject_batch(specs, message)
            self._queued_order_refs.difference_update(item.order_ref for item in specs)
        while True:
            try:
                refs = self._cancel_queue.get_nowait()
            except queue.Empty:
                break
            for order_ref in refs:
                self._emit_order_event(
                    {"event": "cancelRejected", "order_ref": order_ref, "message": message}
                )

    def shutdown(self, timeout_ms: int = 3000) -> None:
        self.disconnect_tws()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout_ms / 1000)
