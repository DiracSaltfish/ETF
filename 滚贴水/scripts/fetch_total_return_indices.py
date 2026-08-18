from __future__ import annotations

import argparse
import json
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yaml


ENDPOINT = "https://www.csindex.com.cn/csindex-home/perf/index-perf"


def fetch_index(index_code: str, start_date: str, end_date: str) -> tuple[pd.DataFrame, str]:
    params = urllib.parse.urlencode(
        {
            "indexCode": index_code,
            "startDate": start_date.replace("-", ""),
            "endDate": end_date.replace("-", ""),
        }
    )
    url = f"{ENDPOINT}?{params}"
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 roll-discount-research/1.1",
        },
    )
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                payload: dict[str, Any] = json.load(response)
            if str(payload.get("code")) != "200":
                raise RuntimeError(f"CSI API error for {index_code}: {payload}")
            rows = payload.get("data") or []
            frame = pd.DataFrame(rows)
            if frame.empty:
                raise RuntimeError(f"CSI API returned no rows for {index_code}")
            required = {"tradeDate", "indexCode", "indexNameCnAll", "close"}
            missing = required.difference(frame.columns)
            if missing:
                raise RuntimeError(f"CSI API response missing columns: {sorted(missing)}")
            result = frame.rename(
                columns={
                    "tradeDate": "date",
                    "indexCode": "index_code",
                    "indexNameCnAll": "index_name",
                }
            )[["date", "index_code", "index_name", "close"]].copy()
            result["date"] = pd.to_datetime(result["date"], format="%Y%m%d", errors="raise")
            result["close"] = pd.to_numeric(result["close"], errors="raise")
            result = result.sort_values("date").drop_duplicates("date", keep="last")
            if set(result["index_code"].astype(str)) != {index_code}:
                raise RuntimeError(f"Unexpected index code in response for {index_code}")
            if result["close"].isna().any() or (result["close"] <= 0).any():
                raise RuntimeError(f"Invalid close values in response for {index_code}")
            result["date"] = result["date"].dt.strftime("%Y-%m-%d")
            result["source_url"] = url
            result["retrieved_at_utc"] = datetime.now(timezone.utc).isoformat()
            return result, url
        except Exception as error:  # network retry boundary
            last_error = error
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch {index_code}") from last_error


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch official CSI gross total-return indices")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--start", default="2010-01-01")
    parser.add_argument("--end")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    with config_path.open("r", encoding="utf-8") as handle:
        config = yaml.safe_load(handle)
    output_dir = (config_path.parent / config["benchmark_data_dir"]).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    end_date = args.end or config["end_date"]

    records: list[dict[str, Any]] = []
    for product, values in config["products"].items():
        index_code = str(values["total_return_symbol"])
        frame, source_url = fetch_index(index_code, args.start, end_date)
        output_path = output_dir / f"{index_code}.csv"
        frame.to_csv(output_path, index=False, encoding="utf-8-sig")
        records.append(
            {
                "product": product,
                "index_code": index_code,
                "rows": int(len(frame)),
                "start_date": frame["date"].min(),
                "end_date": frame["date"].max(),
                "source_url": source_url,
                "output_path": str(output_path),
            }
        )
    print(pd.DataFrame(records).to_string(index=False))


if __name__ == "__main__":
    main()
