from __future__ import annotations

import json
import os
import threading
import fcntl
from datetime import datetime
from functools import wraps
from pathlib import Path

from basket_models import SubmittedOrder
from close_only import (
    CloseCampaignBasis,
    CloseOnlyPlan,
    campaign_basis_from_dict,
    campaign_basis_to_dict,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_STORE_PATH = ROOT / "close_only_sessions.json"
_LOCK = threading.RLock()


def _exclusive_store_update(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        path = store_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(path.suffix + ".lock")
        with _LOCK, lock_path.open("a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                return function(*args, **kwargs)
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    return wrapped


def store_path() -> Path:
    override = str(os.environ.get("BASKET_CLOSE_STORE_PATH") or "").strip()
    return Path(override).expanduser().resolve() if override else DEFAULT_STORE_PATH


def _empty_store() -> dict[str, object]:
    return {"version": 1, "campaigns": []}


def load_store() -> dict[str, object]:
    path = store_path()
    with _LOCK:
        if not path.exists():
            return _empty_store()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Close Only 会话文件无法读取: {path}: {exc}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("campaigns"), list)
    ):
        raise ValueError(f"Close Only 会话文件格式无效: {path}")
    return payload


def _save_store(payload: dict[str, object]) -> None:
    path = store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    with _LOCK:
        temp_path.write_text(encoded, encoding="utf-8")
        temp_path.replace(path)


def load_active_campaign_basis(
    *,
    account: str,
    base_symbol: str,
    basket_path: str,
) -> CloseCampaignBasis | None:
    payload = load_store()
    campaigns = payload.get("campaigns") or []
    candidates = [
        campaign
        for campaign in campaigns
        if isinstance(campaign, dict)
        and campaign.get("account") == account
        and campaign.get("base_symbol") == base_symbol
        and campaign.get("basket_path") == basket_path
        and campaign.get("status") not in {"COMPLETE", "ABANDONED"}
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        campaign_ids = ", ".join(str(item.get("campaign_id") or "?") for item in candidates)
        raise ValueError(f"发现多个未完成 Close Only 会话，禁止自动选择: {campaign_ids}")
    latest = sorted(candidates, key=lambda item: str(item.get("updated_at") or ""))[-1]
    basis_payload = latest.get("basis")
    return campaign_basis_from_dict(basis_payload) if isinstance(basis_payload, dict) else None


def load_active_campaign_orders(
    *,
    account: str,
    base_symbol: str,
    basket_path: str,
) -> tuple[str, tuple[SubmittedOrder, ...]] | None:
    payload = load_store()
    candidates = [
        campaign
        for campaign in payload.get("campaigns") or []
        if isinstance(campaign, dict)
        and campaign.get("account") == account
        and campaign.get("base_symbol") == base_symbol
        and campaign.get("basket_path") == basket_path
        and campaign.get("status") not in {"COMPLETE", "ABANDONED"}
    ]
    if not candidates:
        return None
    if len(candidates) > 1:
        campaign_ids = ", ".join(str(item.get("campaign_id") or "?") for item in candidates)
        raise ValueError(f"发现多个未完成 Close Only 会话，禁止自动选择: {campaign_ids}")
    campaign = candidates[0]
    orders: list[SubmittedOrder] = []
    for item in campaign.get("orders") or []:
        if not isinstance(item, dict):
            raise ValueError("Close Only 会话订单记录格式无效")
        symbol = str(item.get("symbol") or "").upper()
        action = str(item.get("action") or "").upper()
        quantity = int(item.get("quantity") or 0)
        con_id = int(item.get("con_id") or 0)
        order_ref = str(item.get("order_ref") or "")
        if not symbol or action not in {"BUY", "SELL"} or quantity <= 0 or con_id <= 0 or not order_ref:
            raise ValueError("Close Only 会话订单记录缺少必要字段")
        orders.append(
            SubmittedOrder(
                symbol=symbol,
                action=action,
                quantity=quantity,
                order_type=str(item.get("order_type") or ""),
                tif=str(item.get("tif") or ""),
                limit_price=(
                    float(item["limit_price"])
                    if item.get("limit_price") is not None
                    else None
                ),
                order_id=int(item.get("order_id") or 0),
                perm_id=int(item.get("perm_id") or 0),
                status=str(item.get("status") or "Unknown"),
                con_id=con_id,
                order_ref=order_ref,
            )
        )
    return str(campaign.get("campaign_id") or ""), tuple(orders)


@_exclusive_store_update
def ensure_campaign(plan: CloseOnlyPlan, *, basket_path: str) -> None:
    payload = load_store()
    campaigns = payload.setdefault("campaigns", [])
    now_text = datetime.now().astimezone().isoformat(timespec="seconds")
    for campaign in campaigns:
        if isinstance(campaign, dict) and campaign.get("campaign_id") == plan.basis.campaign_id:
            campaign["updated_at"] = now_text
            campaign["last_plan_id"] = plan.plan_id
            campaign["last_fingerprint"] = plan.approval_fingerprint
            _save_store(payload)
            return
    conflicting = [
        campaign
        for campaign in campaigns
        if isinstance(campaign, dict)
        and campaign.get("account") == plan.account
        and campaign.get("base_symbol") == plan.base_symbol
        and campaign.get("basket_path") == basket_path
        and campaign.get("status") not in {"COMPLETE", "ABANDONED"}
    ]
    if conflicting:
        ids = ", ".join(str(item.get("campaign_id") or "?") for item in conflicting)
        raise ValueError(f"已有其他未完成 Close Only 会话，禁止新建: {ids}")
    campaigns.append(
        {
            "campaign_id": plan.basis.campaign_id,
            "account": plan.account,
            "base_symbol": plan.base_symbol,
            "basket_path": basket_path,
            "created_at": plan.basis.created_at,
            "updated_at": now_text,
            "status": "CONFIRMED",
            "basis": campaign_basis_to_dict(plan.basis),
            "last_plan_id": plan.plan_id,
            "last_fingerprint": plan.approval_fingerprint,
            "events": [],
            "orders": [],
        }
    )
    _save_store(payload)


@_exclusive_store_update
def record_event(
    campaign_id: str,
    *,
    event: str,
    message: str = "",
    status: str | None = None,
) -> None:
    payload = load_store()
    now_text = datetime.now().astimezone().isoformat(timespec="seconds")
    for campaign in payload.get("campaigns") or []:
        if not isinstance(campaign, dict) or campaign.get("campaign_id") != campaign_id:
            continue
        campaign.setdefault("events", []).append(
            {"time": now_text, "event": event, "message": message}
        )
        campaign["updated_at"] = now_text
        if status:
            campaign["status"] = status
        _save_store(payload)
        return
    raise ValueError(f"未找到 Close Only 会话 {campaign_id}")


@_exclusive_store_update
def record_submitted_order(
    campaign_id: str,
    *,
    plan_id: str,
    order: SubmittedOrder,
) -> None:
    payload = load_store()
    now_text = datetime.now().astimezone().isoformat(timespec="seconds")
    for campaign in payload.get("campaigns") or []:
        if not isinstance(campaign, dict) or campaign.get("campaign_id") != campaign_id:
            continue
        orders = campaign.setdefault("orders", [])
        identity = (int(order.perm_id), str(order.order_ref))
        if not any(
            (int(item.get("perm_id") or 0), str(item.get("order_ref") or "")) == identity
            for item in orders
            if isinstance(item, dict)
        ):
            orders.append(
                {
                    "recorded_at": now_text,
                    "plan_id": plan_id,
                    "symbol": order.symbol,
                    "con_id": order.con_id,
                    "action": order.action,
                    "quantity": order.quantity,
                    "order_type": order.order_type,
                    "tif": order.tif,
                    "limit_price": order.limit_price,
                    "order_id": order.order_id,
                    "perm_id": order.perm_id,
                    "status": order.status,
                    "order_ref": order.order_ref,
                }
            )
        campaign["status"] = "WORKING"
        campaign["updated_at"] = now_text
        _save_store(payload)
        return
    raise ValueError(f"未找到 Close Only 会话 {campaign_id}")


def mark_campaign_complete(campaign_id: str, message: str = "全部策略仓位为 0") -> None:
    record_event(campaign_id, event="COMPLETE", message=message, status="COMPLETE")
