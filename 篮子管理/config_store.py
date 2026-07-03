from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"

DEFAULT_CONFIG: dict[str, object] = {
    "host": "127.0.0.1",
    "port": 7496,
    "client_id": 9701,
    "account": "",
    "basket_path": "",
    "order_type": "MKT",
    "tif": "DAY",
    "outside_rth": False,
    "limit_buffer_bps": 10,
}


def load_config() -> dict[str, object]:
    config = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            payload = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            config.update(payload)
    return config


def save_config(config: dict[str, object]) -> None:
    CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
